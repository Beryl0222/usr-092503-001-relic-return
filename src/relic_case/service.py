"""案件服务：不可跳步的状态规则、职责分离、幂等、撤销与审计投影。

设计要点：
- 事件溯源：所有变化先写入事件存储，再折叠为内存投影；重启后重放恢复。
- 不可跳步：每个动作都有允许的执行阶段，错序一律拒绝。
- 幂等：外部函件/回执以外部标识登记，重复送达返回首次结果，不重复推进。
- 职责分离：复核/批准人不得出现在原鉴定的参与者名单中。
- 争议受限：受限期间除解除争议外的一切变更都被拒绝。
- 撤销不抹史：撤销以新事件标记目标事件，历史完整保留在台账中。
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from .contracts import Action, CaseStage, MaterialKind, Role, validate_external_id
from .errors import (
    ConflictError,
    ForbiddenError,
    InvalidTransitionError,
    NotFoundError,
    OnHoldError,
    UnknownActorError,
    ValidationError,
)
from .events import Event, EventType
from .store import DuplicateExternalId, EventStore

SYSTEM_CASE_ID = "__system__"

_ACTION_ROLES: dict[Action, frozenset[Role]] = {
    Action.SEAL_EVIDENCE: frozenset({Role.CASE_OFFICER, Role.CUSTODIAN}),
    Action.SUBMIT_APPRAISAL: frozenset({Role.EXPERT}),
    Action.APPROVE_ATTRIBUTION: frozenset({Role.REVIEWER}),
    Action.REJECT_ATTRIBUTION: frozenset({Role.REVIEWER}),
    Action.DRAFT_REQUEST: frozenset({Role.CASE_OFFICER}),
    Action.APPROVE_REQUEST: frozenset({Role.APPROVER}),
    Action.RECORD_CONFIRMATION: frozenset({Role.CASE_OFFICER}),
    Action.RECORD_HANDOVER: frozenset({Role.CUSTODIAN}),
    Action.CLOSE_CASE: frozenset({Role.APPROVER}),
    Action.RAISE_DISPUTE: frozenset(Role),
    Action.RESOLVE_DISPUTE: frozenset({Role.REVIEWER, Role.APPROVER}),
}

# 每个动作允许执行的阶段，保证流程不可跳过。
_ACTION_STAGES: dict[Action, tuple[CaseStage, ...]] = {
    Action.SEAL_EVIDENCE: (CaseStage.INTAKE,),
    Action.SUBMIT_APPRAISAL: (CaseStage.EVIDENCE_SEALED,),
    Action.APPROVE_ATTRIBUTION: (CaseStage.EVIDENCE_SEALED,),
    Action.REJECT_ATTRIBUTION: (CaseStage.EVIDENCE_SEALED,),
    Action.DRAFT_REQUEST: (CaseStage.ATTRIBUTION,),
    Action.APPROVE_REQUEST: (CaseStage.ATTRIBUTION,),
    Action.RECORD_CONFIRMATION: (CaseStage.REQUESTED,),
    Action.RECORD_HANDOVER: (CaseStage.CONFIRMED,),
    Action.CLOSE_CASE: (CaseStage.HANDOVER,),
}

# 只有“录入类”事件允许撤销；决定类事件只能留在历史中。
_VOIDABLE_TYPES = frozenset(
    {
        EventType.MATERIAL_SUBMITTED,
        EventType.APPRAISAL_SUBMITTED,
        EventType.CONFIRMATION_RECORDED,
        EventType.HANDOVER_RECORDED,
    }
)

# 动作事件隐含创建的材料类别。
_EVENT_MATERIAL_KIND = {
    EventType.APPRAISAL_SUBMITTED: MaterialKind.EXPERT_OPINION,
    EventType.REQUEST_DRAFTED: MaterialKind.DIPLOMATIC_NOTE,
    EventType.CONFIRMATION_RECORDED: MaterialKind.DIPLOMATIC_NOTE,
    EventType.HANDOVER_RECORDED: MaterialKind.HANDOVER_RECEIPT,
}

_STAGE_LABELS = {
    CaseStage.INTAKE: "线索登记",
    CaseStage.EVIDENCE_SEALED: "证据封存",
    CaseStage.ATTRIBUTION: "专家鉴定",
    CaseStage.REQUESTED: "对外请求",
    CaseStage.CONFIRMED: "对方确认",
    CaseStage.HANDOVER: "运输交接",
    CaseStage.ARCHIVED: "结案归档",
}


class _CaseState:
    """单案件的内存投影，由事件流折叠而成。"""

    def __init__(self, case_id: str):
        self.case_id = case_id
        self.title = ""
        self.relic: dict = {}
        self.stage: CaseStage | None = None
        self.held = False
        self.hold_reason: str | None = None
        self.materials: dict[str, dict] = {}
        self.voided_materials: dict[str, dict] = {}
        self.kind_counters: dict[str, int] = {}
        self.pending_appraisal: dict | None = None
        self.attribution_participants: set[str] = set()
        self.pending_request: dict | None = None
        self.chain: dict[str, dict | None] = {
            "request": None,
            "confirmation": None,
            "receipt": None,
        }
        self.events: list[Event] = []
        self.created_at: str | None = None
        self.updated_at: str | None = None


class CaseService:
    """线程安全的案件服务；所有写操作经同一把锁串行化。"""

    def __init__(self, store: EventStore, clock=None):
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._actors: dict[str, dict] = {}
        self._cases: dict[str, _CaseState] = {}
        for event in self._store.load_all():
            self._apply(event)

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._store.close()

    def _now(self) -> str:
        moment = self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")

    def _new_event(
        self,
        case_id: str,
        event_type: EventType,
        actor_id: str,
        role: str | None,
        payload: dict,
    ) -> Event:
        return Event(
            event_id=f"evt-{uuid.uuid4().hex}",
            case_id=case_id,
            type=event_type,
            actor_id=actor_id,
            role=role,
            occurred_at=self._now(),
            payload=payload,
        )

    def _commit(self, event: Event, *, external_id: str | None = None) -> None:
        try:
            self._store.append(event, external_id=external_id)
        except DuplicateExternalId:
            raise ConflictError(f"外部标识 {external_id} 已存在") from None
        self._apply(event)

    def _require_actor(self, actor_id: str | None) -> dict:
        if not actor_id or actor_id not in self._actors:
            raise UnknownActorError("操作者未登记或缺少身份标识")
        return self._actors[actor_id]

    def _require_case(self, case_id: str) -> _CaseState:
        case = self._cases.get(case_id)
        if case is None or case.stage is None:
            raise NotFoundError(f"案件 {case_id} 不存在")
        return case

    def _require_role(self, actor: dict, action: Action) -> str:
        allowed = _ACTION_ROLES[action]
        matched = sorted(r.value for r in allowed if r.value in actor["roles"])
        if not matched:
            need = "/".join(sorted(r.value for r in allowed))
            raise ForbiddenError(f"动作 {action.value} 需要职责 {need}")
        return matched[0]

    def _ensure_mutable(self, case: _CaseState) -> None:
        if case.held:
            raise OnHoldError("案件处于争议受限状态，须先解除争议")
        if case.stage == CaseStage.ARCHIVED:
            raise InvalidTransitionError("案件已结案归档，禁止变更")

    def _ensure_stage(self, case: _CaseState, action: Action) -> None:
        allowed = _ACTION_STAGES[action]
        if case.stage not in allowed:
            want = "/".join(_STAGE_LABELS[s] for s in allowed)
            raise InvalidTransitionError(
                f"当前阶段为「{_STAGE_LABELS[case.stage]}」，"
                f"动作 {action.value} 仅可在「{want}」阶段执行"
            )

    def _check_external_id(
        self, case: _CaseState, external_id: str, expected_type: EventType
    ) -> dict | None:
        """外部标识幂等检查：已登记则返回登记行，冲突则抛错。"""

        existing = self._store.lookup_external(external_id)
        if existing is None:
            return None
        if existing["case_id"] != case.case_id:
            raise ConflictError(
                f"外部标识 {external_id} 已被案件 {existing['case_id']} 使用"
            )
        if existing["type"] != expected_type.value:
            raise ConflictError(f"外部标识 {external_id} 已用于本案的其他记录")
        return existing

    @staticmethod
    def _validate_external_id(external_id: str) -> None:
        try:
            validate_external_id(external_id)
        except ValueError as exc:
            raise ValidationError(str(exc)) from None

    def _material_refs(self, case: _CaseState) -> list[dict]:
        """快照当前有效材料版本，作为决定的依据留存。"""

        return [
            {
                "material_id": m["material_id"],
                "kind": m["kind"],
                "version": m["version"],
            }
            for m in case.materials.values()
            if m["status"] == "active"
        ]

    def _result(
        self,
        case: _CaseState,
        action: Action,
        event: Event | None,
        *,
        deduplicated: bool = False,
        original_event_id: str | None = None,
    ) -> dict:
        return {
            "action": action.value,
            "deduplicated": deduplicated,
            "event_id": event.event_id if event else original_event_id,
            "status": self._status(case),
        }

    # ------------------------------------------------------------------
    # 操作者与案件登记
    # ------------------------------------------------------------------

    def register_actor(self, actor_id: str, name: str, org: str, roles: list[str]) -> dict:
        with self._lock:
            if not actor_id or not str(actor_id).strip():
                raise ValidationError("actor_id 不能为空")
            if not name or not str(name).strip():
                raise ValidationError("name 不能为空")
            try:
                role_set = {Role(r) for r in roles}
            except ValueError as exc:
                raise ValidationError(f"未知职责: {exc}") from None
            if not role_set:
                raise ValidationError("至少需要一个职责")
            existing = self._actors.get(actor_id)
            if existing is not None:
                same = (
                    existing["name"] == name
                    and existing["org"] == org
                    and set(existing["roles"]) == {r.value for r in role_set}
                )
                if same:
                    return {"actor": dict(existing), "deduplicated": True}
                raise ConflictError(f"操作者 {actor_id} 已登记且资料不一致")
            event = self._new_event(
                SYSTEM_CASE_ID,
                EventType.ACTOR_REGISTERED,
                actor_id,
                None,
                {
                    "actor_id": actor_id,
                    "name": name,
                    "org": org,
                    "roles": sorted(r.value for r in role_set),
                },
            )
            self._commit(event)
            return {"actor": dict(self._actors[actor_id]), "deduplicated": False}

    def list_actors(self) -> list[dict]:
        with self._lock:
            return [dict(a) for a in self._actors.values()]

    def create_case(
        self,
        actor_id: str,
        title: str,
        relic: dict,
        case_id: str | None = None,
    ) -> dict:
        with self._lock:
            actor = self._require_actor(actor_id)
            if Role.CASE_OFFICER.value not in actor["roles"]:
                raise ForbiddenError("只有办案人可以登记案件")
            if not title or not str(title).strip():
                raise ValidationError("案件标题不能为空")
            if not isinstance(relic, dict) or not relic:
                raise ValidationError("文物信息不能为空")
            if case_id is None:
                case_id = f"case-{uuid.uuid4().hex[:12]}"
            else:
                self._validate_external_id(case_id)
            if case_id in self._cases:
                raise ConflictError(f"案件 {case_id} 已存在")
            event = self._new_event(
                case_id,
                EventType.CASE_CREATED,
                actor_id,
                Role.CASE_OFFICER.value,
                {"title": title, "relic": relic},
            )
            self._commit(event)
            return self._status(self._cases[case_id])

    # ------------------------------------------------------------------
    # 材料录入
    # ------------------------------------------------------------------

    def submit_material(
        self,
        actor_id: str,
        case_id: str,
        kind: str,
        summary: str,
        uri: str | None = None,
        external_id: str | None = None,
    ) -> dict:
        with self._lock:
            actor = self._require_actor(actor_id)
            case = self._require_case(case_id)
            try:
                kind = MaterialKind(kind)
            except ValueError:
                raise ValidationError(f"未知材料类别: {kind}") from None
            if not summary or not str(summary).strip():
                raise ValidationError("材料摘要不能为空")
            if external_id is not None:
                self._validate_external_id(external_id)
                existing = self._check_external_id(
                    case, external_id, EventType.MATERIAL_SUBMITTED
                )
                if existing is not None:
                    material = self._material_of_event(case, existing["event_id"])
                    return {
                        "material": material,
                        "deduplicated": True,
                        "status": self._status(case),
                    }
            self._ensure_mutable(case)
            payload = {
                "material_id": f"mat-{uuid.uuid4().hex[:12]}",
                "kind": kind.value,
                "summary": summary,
                "uri": uri,
                "external_id": external_id,
            }
            event = self._new_event(
                case_id, EventType.MATERIAL_SUBMITTED, actor["actor_id"], None, payload
            )
            self._commit(event, external_id=external_id)
            return {
                "material": dict(case.materials[payload["material_id"]]),
                "deduplicated": False,
                "status": self._status(case),
            }

    def _material_of_event(self, case: _CaseState, event_id: str) -> dict | None:
        for material in case.materials.values():
            if material["event_id"] == event_id:
                return dict(material)
        return None

    # ------------------------------------------------------------------
    # 动作分发
    # ------------------------------------------------------------------

    def perform(
        self,
        actor_id: str,
        case_id: str,
        action: str,
        params: dict | None = None,
    ) -> dict:
        try:
            action = Action(action)
        except ValueError:
            raise ValidationError(f"未知动作: {action}") from None
        params = params or {}
        with self._lock:
            actor = self._require_actor(actor_id)
            case = self._require_case(case_id)
            role = self._require_role(actor, action)
            handler = getattr(self, f"_do_{action.value}")
            return handler(actor, role, case, params)

    def _do_seal_evidence(self, actor, role, case, params) -> dict:
        self._ensure_mutable(case)
        self._ensure_stage(case, Action.SEAL_EVIDENCE)
        has_record = any(
            m["kind"] == MaterialKind.SEIZURE_RECORD and m["status"] == "active"
            for m in case.materials.values()
        )
        if not has_record:
            raise InvalidTransitionError(
                "证据封存前至少需要一份查封记录材料（seizure_record）"
            )
        event = self._new_event(
            case.case_id,
            EventType.EVIDENCE_SEALED,
            actor["actor_id"],
            role,
            {"material_refs": self._material_refs(case)},
        )
        self._commit(event)
        return self._result(case, Action.SEAL_EVIDENCE, event)

    def _do_submit_appraisal(self, actor, role, case, params) -> dict:
        self._ensure_mutable(case)
        self._ensure_stage(case, Action.SUBMIT_APPRAISAL)
        if case.pending_appraisal is not None:
            raise ConflictError("已存在待复核的鉴定意见")
        conclusion = str(params.get("conclusion") or "").strip()
        if not conclusion:
            raise ValidationError("鉴定结论不能为空")
        participants = [actor["actor_id"]]
        for co in params.get("co_authors") or []:
            if co not in participants:
                participants.append(co)
        payload = {
            "material_id": f"mat-{uuid.uuid4().hex[:12]}",
            "summary": str(params.get("summary") or conclusion),
            "uri": params.get("uri"),
            "conclusion": conclusion,
            "participants": participants,
        }
        event = self._new_event(
            case.case_id, EventType.APPRAISAL_SUBMITTED, actor["actor_id"], role, payload
        )
        self._commit(event)
        return self._result(case, Action.SUBMIT_APPRAISAL, event)

    def _do_approve_attribution(self, actor, role, case, params) -> dict:
        self._ensure_mutable(case)
        self._ensure_stage(case, Action.APPROVE_ATTRIBUTION)
        pending = case.pending_appraisal
        if pending is None:
            raise ConflictError("没有待复核的鉴定意见")
        if actor["actor_id"] in pending["participants"]:
            raise ForbiddenError("复核人不得参与原鉴定")
        payload = {
            "opinion_material_id": pending["material_id"],
            "material_refs": self._material_refs(case),
        }
        event = self._new_event(
            case.case_id,
            EventType.ATTRIBUTION_APPROVED,
            actor["actor_id"],
            role,
            payload,
        )
        self._commit(event)
        return self._result(case, Action.APPROVE_ATTRIBUTION, event)

    def _do_reject_attribution(self, actor, role, case, params) -> dict:
        self._ensure_mutable(case)
        self._ensure_stage(case, Action.REJECT_ATTRIBUTION)
        pending = case.pending_appraisal
        if pending is None:
            raise ConflictError("没有待复核的鉴定意见")
        if actor["actor_id"] in pending["participants"]:
            raise ForbiddenError("复核人不得参与原鉴定")
        reason = str(params.get("reason") or "").strip()
        if not reason:
            raise ValidationError("驳回原因不能为空")
        payload = {
            "opinion_material_id": pending["material_id"],
            "reason": reason,
            "material_refs": self._material_refs(case),
        }
        event = self._new_event(
            case.case_id,
            EventType.ATTRIBUTION_REJECTED,
            actor["actor_id"],
            role,
            payload,
        )
        self._commit(event)
        return self._result(case, Action.REJECT_ATTRIBUTION, event)

    def _do_draft_request(self, actor, role, case, params) -> dict:
        external_id = params.get("external_id")
        if external_id is not None:
            self._validate_external_id(external_id)
            existing = self._check_external_id(case, external_id, EventType.REQUEST_DRAFTED)
            if existing is not None:
                return self._result(
                    case,
                    Action.DRAFT_REQUEST,
                    None,
                    deduplicated=True,
                    original_event_id=existing["event_id"],
                )
        self._ensure_mutable(case)
        self._ensure_stage(case, Action.DRAFT_REQUEST)
        if case.pending_request is not None:
            raise ConflictError("已存在待批准的对外请求草稿")
        summary = str(params.get("summary") or "").strip()
        if not summary:
            raise ValidationError("对外请求函摘要不能为空")
        payload = {
            "material_id": f"mat-{uuid.uuid4().hex[:12]}",
            "summary": summary,
            "uri": params.get("uri"),
            "external_id": external_id,
            "material_refs": self._material_refs(case),
        }
        event = self._new_event(
            case.case_id, EventType.REQUEST_DRAFTED, actor["actor_id"], role, payload
        )
        self._commit(event, external_id=external_id)
        return self._result(case, Action.DRAFT_REQUEST, event)

    def _do_approve_request(self, actor, role, case, params) -> dict:
        self._ensure_mutable(case)
        self._ensure_stage(case, Action.APPROVE_REQUEST)
        if case.pending_request is None:
            raise ConflictError("没有待批准的对外请求")
        if actor["actor_id"] in case.attribution_participants:
            raise ForbiddenError("批准人不得参与原鉴定")
        payload = {
            "note_material_id": case.pending_request["material_id"],
            "material_refs": self._material_refs(case),
        }
        event = self._new_event(
            case.case_id, EventType.REQUEST_APPROVED, actor["actor_id"], role, payload
        )
        self._commit(event)
        return self._result(case, Action.APPROVE_REQUEST, event)

    def _do_record_confirmation(self, actor, role, case, params) -> dict:
        external_id = params.get("external_id")
        if not external_id:
            raise ValidationError("对方确认函必须提供 external_id")
        self._validate_external_id(external_id)
        existing = self._check_external_id(
            case, external_id, EventType.CONFIRMATION_RECORDED
        )
        if existing is not None:
            return self._result(
                case,
                Action.RECORD_CONFIRMATION,
                None,
                deduplicated=True,
                original_event_id=existing["event_id"],
            )
        self._ensure_mutable(case)
        self._ensure_stage(case, Action.RECORD_CONFIRMATION)
        payload = {
            "material_id": f"mat-{uuid.uuid4().hex[:12]}",
            "summary": str(params.get("summary") or "对方确认函"),
            "uri": params.get("uri"),
            "external_id": external_id,
            "material_refs": self._material_refs(case),
        }
        event = self._new_event(
            case.case_id,
            EventType.CONFIRMATION_RECORDED,
            actor["actor_id"],
            role,
            payload,
        )
        self._commit(event, external_id=external_id)
        return self._result(case, Action.RECORD_CONFIRMATION, event)

    def _do_record_handover(self, actor, role, case, params) -> dict:
        external_id = params.get("external_id")
        if not external_id:
            raise ValidationError("交接回执必须提供 external_id")
        self._validate_external_id(external_id)
        existing = self._check_external_id(case, external_id, EventType.HANDOVER_RECORDED)
        if existing is not None:
            return self._result(
                case,
                Action.RECORD_HANDOVER,
                None,
                deduplicated=True,
                original_event_id=existing["event_id"],
            )
        self._ensure_mutable(case)
        self._ensure_stage(case, Action.RECORD_HANDOVER)
        payload = {
            "material_id": f"mat-{uuid.uuid4().hex[:12]}",
            "summary": str(params.get("summary") or "交接回执"),
            "uri": params.get("uri"),
            "external_id": external_id,
            "material_refs": self._material_refs(case),
        }
        event = self._new_event(
            case.case_id, EventType.HANDOVER_RECORDED, actor["actor_id"], role, payload
        )
        self._commit(event, external_id=external_id)
        return self._result(case, Action.RECORD_HANDOVER, event)

    def _do_close_case(self, actor, role, case, params) -> dict:
        self._ensure_mutable(case)
        self._ensure_stage(case, Action.CLOSE_CASE)
        if actor["actor_id"] in case.attribution_participants:
            raise ForbiddenError("批准人不得参与原鉴定")
        if not (case.chain["confirmation"] and case.chain["receipt"]):
            raise ConflictError("交接链未闭合，无法结案归档")
        event = self._new_event(
            case.case_id,
            EventType.CASE_CLOSED,
            actor["actor_id"],
            role,
            {"material_refs": self._material_refs(case)},
        )
        self._commit(event)
        return self._result(case, Action.CLOSE_CASE, event)

    def _do_raise_dispute(self, actor, role, case, params) -> dict:
        if case.stage == CaseStage.ARCHIVED:
            raise InvalidTransitionError("案件已结案归档，无法发起争议")
        if case.held:
            raise ConflictError("案件已处于争议受限状态")
        reason = str(params.get("reason") or "").strip()
        if not reason:
            raise ValidationError("争议原因不能为空")
        event = self._new_event(
            case.case_id,
            EventType.DISPUTE_RAISED,
            actor["actor_id"],
            role,
            {"reason": reason, "material_refs": self._material_refs(case)},
        )
        self._commit(event)
        return self._result(case, Action.RAISE_DISPUTE, event)

    def _do_resolve_dispute(self, actor, role, case, params) -> dict:
        if not case.held:
            raise InvalidTransitionError("案件未处于争议受限状态")
        event = self._new_event(
            case.case_id,
            EventType.DISPUTE_RESOLVED,
            actor["actor_id"],
            role,
            {
                "note": str(params.get("note") or ""),
                "material_refs": self._material_refs(case),
            },
        )
        self._commit(event)
        return self._result(case, Action.RESOLVE_DISPUTE, event)

    # ------------------------------------------------------------------
    # 撤销（不抹去历史）
    # ------------------------------------------------------------------

    def void_event(self, actor_id: str, case_id: str, event_id: str, reason: str) -> dict:
        with self._lock:
            actor = self._require_actor(actor_id)
            case = self._require_case(case_id)
            if Role.CASE_OFFICER.value not in actor["roles"]:
                raise ForbiddenError("只有办案人可以撤销录入")
            self._ensure_mutable(case)
            if not reason or not str(reason).strip():
                raise ValidationError("撤销原因不能为空")
            target = next(
                (e for e in case.events if e.event_id == event_id), None
            )
            if target is None:
                raise NotFoundError(f"事件 {event_id} 不存在")
            if target.voided_by is not None:
                raise ConflictError("该事件已被撤销")
            if target.type not in _VOIDABLE_TYPES:
                raise ConflictError(f"事件类型 {target.type.value} 不可撤销")
            effective = [
                e
                for e in case.events
                if e.type != EventType.EVENT_VOIDED and e.voided_by is None
            ]
            if effective and effective[-1] is not target:
                raise ConflictError("只能撤销最新一笔录入")
            void = self._new_event(
                case_id,
                EventType.EVENT_VOIDED,
                actor["actor_id"],
                Role.CASE_OFFICER.value,
                {
                    "target_event_id": target.event_id,
                    "target_type": target.type.value,
                    "reason": reason,
                    "material_refs": self._material_refs(case),
                },
            )
            self._store.mark_voided(void, target.event_id)
            self._apply(void)
            return {"voided_event_id": target.event_id, "status": self._status(case)}

    # ------------------------------------------------------------------
    # 查询与导出
    # ------------------------------------------------------------------

    def get_case(self, case_id: str) -> dict:
        with self._lock:
            return self._status(self._require_case(case_id))

    def list_cases(self) -> list[dict]:
        with self._lock:
            cases = sorted(
                self._cases.values(), key=lambda c: (c.created_at or "", c.case_id)
            )
            return [self._summary(c) for c in cases]

    def list_events(self, case_id: str) -> list[dict]:
        with self._lock:
            case = self._require_case(case_id)
            return [e.to_dict() for e in case.events]

    def export_case(self, case_id: str) -> dict:
        """按案件导出完整监管记录：状态、材料版本、全部事件与交接链。"""

        with self._lock:
            case = self._require_case(case_id)
            involved = {e.actor_id for e in case.events}
            actors = {
                aid: dict(self._actors[aid]) for aid in involved if aid in self._actors
            }
            materials = [dict(m) for m in case.materials.values()] + [
                dict(m) for m in case.voided_materials.values()
            ]
            materials.sort(key=lambda m: (m["kind"], m["version"]))
            events = [e.to_dict() for e in case.events]
            return {
                "export_type": "relic_case_regulatory_record",
                "generated_at": self._now(),
                "case": self._status(case),
                "actors": actors,
                "materials": materials,
                "events": events,
                "chain_of_custody": {
                    "request": case.chain["request"],
                    "confirmation": case.chain["confirmation"],
                    "receipt": case.chain["receipt"],
                    "closed": case.stage == CaseStage.ARCHIVED,
                },
                "integrity": {
                    "event_count": len(events),
                    "voided_count": sum(1 for e in case.events if e.voided_by),
                },
            }

    # ------------------------------------------------------------------
    # 投影折叠
    # ------------------------------------------------------------------

    def _apply(self, event: Event) -> None:
        if event.type == EventType.ACTOR_REGISTERED:
            p = event.payload
            self._actors[p["actor_id"]] = {
                "actor_id": p["actor_id"],
                "name": p["name"],
                "org": p["org"],
                "roles": list(p["roles"]),
            }
            return
        case = self._cases.get(event.case_id)
        if case is None:
            case = self._cases[event.case_id] = _CaseState(event.case_id)
        case.events.append(event)
        t, p = event.type, event.payload
        if t == EventType.CASE_CREATED:
            case.title = p["title"]
            case.relic = p["relic"]
            case.stage = CaseStage.INTAKE
            case.created_at = event.occurred_at
        elif t == EventType.MATERIAL_SUBMITTED:
            self._fold_material(case, event, MaterialKind(p["kind"]))
        elif t == EventType.EVIDENCE_SEALED:
            case.stage = CaseStage.EVIDENCE_SEALED
        elif t == EventType.APPRAISAL_SUBMITTED:
            self._fold_material(case, event, MaterialKind.EXPERT_OPINION)
            case.pending_appraisal = {
                "material_id": p["material_id"],
                "participants": list(p["participants"]),
                "conclusion": p["conclusion"],
            }
        elif t == EventType.ATTRIBUTION_APPROVED:
            case.stage = CaseStage.ATTRIBUTION
            if case.pending_appraisal is not None:
                case.attribution_participants = set(
                    case.pending_appraisal["participants"]
                )
            case.pending_appraisal = None
        elif t == EventType.ATTRIBUTION_REJECTED:
            if case.pending_appraisal is not None:
                opinion = case.materials.get(case.pending_appraisal["material_id"])
                if opinion is not None:
                    opinion["status"] = "rejected"
            case.pending_appraisal = None
        elif t == EventType.REQUEST_DRAFTED:
            self._fold_material(
                case, event, MaterialKind.DIPLOMATIC_NOTE, extra={"direction": "outbound"}
            )
            case.pending_request = {
                "material_id": p["material_id"],
                "external_id": p.get("external_id"),
            }
            case.chain["request"] = {
                "external_id": p.get("external_id"),
                "at": event.occurred_at,
                "by": event.actor_id,
                "event_id": event.event_id,
            }
        elif t == EventType.REQUEST_APPROVED:
            case.stage = CaseStage.REQUESTED
            case.pending_request = None
        elif t == EventType.CONFIRMATION_RECORDED:
            self._fold_material(
                case, event, MaterialKind.DIPLOMATIC_NOTE, extra={"direction": "inbound"}
            )
            case.stage = CaseStage.CONFIRMED
            case.chain["confirmation"] = {
                "external_id": p["external_id"],
                "at": event.occurred_at,
                "by": event.actor_id,
                "event_id": event.event_id,
            }
        elif t == EventType.HANDOVER_RECORDED:
            self._fold_material(case, event, MaterialKind.HANDOVER_RECEIPT)
            case.stage = CaseStage.HANDOVER
            case.chain["receipt"] = {
                "external_id": p["external_id"],
                "at": event.occurred_at,
                "by": event.actor_id,
                "event_id": event.event_id,
            }
        elif t == EventType.CASE_CLOSED:
            case.stage = CaseStage.ARCHIVED
        elif t == EventType.DISPUTE_RAISED:
            case.held = True
            case.hold_reason = p["reason"]
        elif t == EventType.DISPUTE_RESOLVED:
            case.held = False
            case.hold_reason = None
        elif t == EventType.EVENT_VOIDED:
            target = next(
                (e for e in case.events if e.event_id == p["target_event_id"]), None
            )
            if target is not None:
                # 恢复场景下 voided_by 已随事件载入，此处幂等标记并回滚效果
                target.voided_by = event.event_id
                self._revert(case, target)
        case.updated_at = event.occurred_at

    def _fold_material(
        self,
        case: _CaseState,
        event: Event,
        kind: MaterialKind,
        *,
        extra: dict | None = None,
    ) -> None:
        p = event.payload
        version = case.kind_counters.get(kind.value, 0) + 1
        case.kind_counters[kind.value] = version
        case.materials[p["material_id"]] = {
            "material_id": p["material_id"],
            "kind": kind.value,
            "version": version,
            "summary": p.get("summary"),
            "uri": p.get("uri"),
            "external_id": p.get("external_id"),
            "status": "active",
            "submitted_by": event.actor_id,
            "submitted_at": event.occurred_at,
            "event_id": event.event_id,
            **(extra or {}),
        }

    def _revert(self, case: _CaseState, target: Event) -> None:
        """撤销目标录入事件的效果；事件本身保留在台账中。"""

        t, p = target.type, target.payload

        def tombstone(material_id: str) -> None:
            material = case.materials.pop(material_id, None)
            if material is not None:
                material["status"] = "voided"
                material["voided_by"] = target.voided_by
                case.voided_materials[material_id] = material

        if t == EventType.MATERIAL_SUBMITTED:
            tombstone(p["material_id"])
        elif t == EventType.APPRAISAL_SUBMITTED:
            tombstone(p["material_id"])
            if (
                case.pending_appraisal is not None
                and case.pending_appraisal["material_id"] == p["material_id"]
            ):
                case.pending_appraisal = None
        elif t == EventType.CONFIRMATION_RECORDED:
            tombstone(p["material_id"])
            case.chain["confirmation"] = None
            case.stage = CaseStage.REQUESTED
        elif t == EventType.HANDOVER_RECORDED:
            tombstone(p["material_id"])
            case.chain["receipt"] = None
            case.stage = CaseStage.CONFIRMED

    # ------------------------------------------------------------------
    # 状态视图
    # ------------------------------------------------------------------

    def _summary(self, case: _CaseState) -> dict:
        return {
            "case_id": case.case_id,
            "title": case.title,
            "stage": case.stage.value if case.stage else None,
            "stage_label": _STAGE_LABELS.get(case.stage),
            "held": case.held,
            "responsible": self._responsible(case),
            "handover_chain_closed": case.stage == CaseStage.ARCHIVED,
            "updated_at": case.updated_at,
        }

    def _status(self, case: _CaseState) -> dict:
        closed = case.stage == CaseStage.ARCHIVED
        return {
            "case_id": case.case_id,
            "title": case.title,
            "relic": case.relic,
            "stage": case.stage.value if case.stage else None,
            "stage_label": _STAGE_LABELS.get(case.stage),
            "held": case.held,
            "hold_reason": case.hold_reason,
            "responsible": self._responsible(case),
            "pending_conditions": self._pending_conditions(case),
            "handover_chain_closed": closed,
            "chain": {
                "request": case.chain["request"],
                "confirmation": case.chain["confirmation"],
                "receipt": case.chain["receipt"],
                "closed": closed,
            },
            "materials": [dict(m) for m in case.materials.values()],
            "created_at": case.created_at,
            "updated_at": case.updated_at,
        }

    def _responsible(self, case: _CaseState) -> dict:
        if case.stage == CaseStage.ARCHIVED:
            return {"role": None, "label": "已结案", "note": "案件已归档，无待办责任方"}
        if case.held:
            return {
                "role": Role.REVIEWER.value,
                "label": "复核人/批准人",
                "note": "争议处理中，需解除受限状态后方可继续",
            }
        stage = case.stage
        if stage == CaseStage.INTAKE:
            return {
                "role": Role.CASE_OFFICER.value,
                "label": "联合工作组办案人",
                "note": "登记线索、补齐查封记录并封存证据",
            }
        if stage == CaseStage.EVIDENCE_SEALED:
            if case.pending_appraisal is None:
                return {
                    "role": Role.EXPERT.value,
                    "label": "鉴定专家",
                    "note": "提交鉴定意见",
                }
            return {
                "role": Role.REVIEWER.value,
                "label": "复核人",
                "note": "复核鉴定意见（不得为原鉴定参与者）",
            }
        if stage == CaseStage.ATTRIBUTION:
            if case.pending_request is None:
                return {
                    "role": Role.CASE_OFFICER.value,
                    "label": "联合工作组办案人",
                    "note": "起草对外请求函",
                }
            return {
                "role": Role.APPROVER.value,
                "label": "批准人",
                "note": "批准对外请求（不得为原鉴定参与者）",
            }
        if stage == CaseStage.REQUESTED:
            return {
                "role": "external_counterparty",
                "label": "对手机构",
                "note": "等待对方确认函送达",
            }
        if stage == CaseStage.CONFIRMED:
            return {
                "role": Role.CUSTODIAN.value,
                "label": "保管机构",
                "note": "安排运输并登记交接回执",
            }
        return {
            "role": Role.APPROVER.value,
            "label": "批准人",
            "note": "交接链已闭合，批准结案归档",
        }

    def _pending_conditions(self, case: _CaseState) -> list[dict]:
        if case.stage == CaseStage.ARCHIVED:
            return []
        if case.held:
            return [
                {
                    "code": "dispute_hold",
                    "message": f"案件处于争议受限状态（{case.hold_reason}），"
                    "需复核人或批准人解除争议（resolve_dispute）",
                }
            ]
        stage = case.stage
        if stage == CaseStage.INTAKE:
            conditions = []
            has_record = any(
                m["kind"] == MaterialKind.SEIZURE_RECORD and m["status"] == "active"
                for m in case.materials.values()
            )
            if not has_record:
                conditions.append(
                    {
                        "code": "need_seizure_record",
                        "message": "缺少查封记录材料（seizure_record）",
                    }
                )
            conditions.append(
                {
                    "code": "seal_evidence",
                    "message": "需办案人或保管人执行证据封存（seal_evidence）",
                }
            )
            return conditions
        if stage == CaseStage.EVIDENCE_SEALED:
            if case.pending_appraisal is None:
                return [
                    {
                        "code": "need_appraisal",
                        "message": "等待专家提交鉴定意见（submit_appraisal）",
                    }
                ]
            return [
                {
                    "code": "review_appraisal",
                    "message": "等待未参与原鉴定的复核人批准（approve_attribution）",
                }
            ]
        if stage == CaseStage.ATTRIBUTION:
            if case.pending_request is None:
                return [
                    {
                        "code": "draft_request",
                        "message": "等待办案人起草对外请求（draft_request）",
                    }
                ]
            return [
                {
                    "code": "approve_request",
                    "message": "等待未参与原鉴定的批准人批准对外请求（approve_request）",
                }
            ]
        if stage == CaseStage.REQUESTED:
            return [
                {
                    "code": "await_confirmation",
                    "message": "等待对方确认函送达（record_confirmation）",
                }
            ]
        if stage == CaseStage.CONFIRMED:
            return [
                {
                    "code": "await_handover",
                    "message": "等待保管人登记运输交接回执（record_handover）",
                }
            ]
        return [
            {
                "code": "close_case",
                "message": "交接链已闭合，等待批准人结案（close_case）",
            }
        ]
