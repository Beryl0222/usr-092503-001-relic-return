"""案件状态机与事件溯源引擎。

所有状态变更都以“只追加事件”落库；当前状态由事件流重放得到。
撤销通过冲正事件实现，原事件保留且在监管台账中可见。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .contracts import CaseStage, MaterialKind, Role, validate_external_id
from .storage import EventStore

# ---------------------------------------------------------------- 错误类型


class DomainError(Exception):
    """业务规则违例；http_status 给出对应 HTTP 状态码。"""

    http_status = 400
    code = "domain_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class NotFoundError(DomainError):
    http_status = 404
    code = "not_found"


class AuthzError(DomainError):
    http_status = 403
    code = "forbidden"


class OrderingError(DomainError):
    http_status = 409
    code = "out_of_order"


class DuplicateEventError(DomainError):
    """外部函件/回执重复送达：不推进流程，只回指首次事件。"""

    http_status = 409
    code = "duplicate_event"

    def __init__(self, message: str, *, original_seq: int, replayed: bool = False):
        super().__init__(message, code="duplicate_event")
        self.original_seq = original_seq
        self.replayed = replayed


# ---------------------------------------------------------------- 常量

STAGE_ORDER = [
    CaseStage.INTAKE,
    CaseStage.EVIDENCE_SEALED,
    CaseStage.ATTRIBUTION,
    CaseStage.REQUESTED,
    CaseStage.HANDOVER,
    CaseStage.ARCHIVED,
]

# 需要独立复核/批准的关键动作 → 允许的职责
APPROVAL_RULES: dict[str, set[Role]] = {
    "issue_request": {Role.APPROVER},
    "complete_handover": {Role.APPROVER, Role.REVIEWER},
    "archive": {Role.REVIEWER, Role.APPROVER},
}

# 各批准动作只允许在对应阶段作出
APPROVAL_STAGE: dict[str, CaseStage] = {
    "issue_request": CaseStage.ATTRIBUTION,
    "complete_handover": CaseStage.REQUESTED,
    "archive": CaseStage.HANDOVER,
}

REVERSIBLE_TYPES = {
    "material_registered",
    "evidence_sealed",
    "attribution_recorded",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# ---------------------------------------------------------------- 投影


@dataclass
class MaterialVersion:
    material_id: str
    version: int
    kind: str
    title: str
    sha256: str | None
    external_ref: str | None
    registered_by: str
    event_seq: int
    reversed: bool = False
    reversed_by_seq: int | None = None


@dataclass
class CaseState:
    case_id: str
    opened: bool = False
    item: dict = field(default_factory=dict)
    team: dict = field(default_factory=dict)
    stage: str = CaseStage.INTAKE
    stage_entries: dict = field(default_factory=dict)
    materials: dict[str, list[MaterialVersion]] = field(default_factory=dict)
    experts: set[str] = field(default_factory=set)
    approvals: dict[str, dict] = field(default_factory=dict)
    links: dict[str, dict] = field(default_factory=dict)
    request_ref: str | None = None
    confirmation_in_reply_to: str | None = None
    receipt_ref: str | None = None
    disputed: bool = False
    dispute_history: list[dict] = field(default_factory=list)
    reversed_seqs: set[int] = field(default_factory=set)
    events: list[dict] = field(default_factory=list)

    # ---- 查询辅助

    def material(self, material_id: str, version: int | None = None):
        versions = self.materials.get(material_id)
        if not versions:
            return None
        if version is None:
            return versions[-1]
        for v in versions:
            if v.version == version:
                return v
        return None

    def has_kind(self, kind: MaterialKind) -> bool:
        for versions in self.materials.values():
            v = versions[-1]
            if v.kind == kind and not v.reversed:
                return True
        return False

    def active_materials(self) -> list[MaterialVersion]:
        return [
            v
            for versions in self.materials.values()
            for v in versions
            if not v.reversed
        ]

    def refs_for(self, refs: list[dict]) -> list[dict]:
        """把请求中的材料引用解析为带版本与摘要的不可变快照。"""
        snapshot = []
        for ref in refs or []:
            mv = self.material(ref["material_id"], ref.get("version"))
            if mv is None:
                raise DomainError(
                    f"材料 {ref['material_id']} 不存在", code="unknown_material"
                )
            if mv.reversed:
                raise DomainError(
                    f"材料 {mv.material_id} v{mv.version} 已被撤销，不能作为决定依据",
                    code="reversed_material",
                )
            snapshot.append(
                {
                    "material_id": mv.material_id,
                    "version": mv.version,
                    "kind": mv.kind,
                    "sha256": mv.sha256,
                    "event_seq": mv.event_seq,
                }
            )
        return snapshot


def _event_dict(row) -> dict:
    return {
        "seq": row["seq"],
        "case_id": row["case_id"],
        "event_type": row["event_type"],
        "actor_id": row["actor_id"],
        "actor_role": row["actor_role"],
        "idempotency_key": row["idempotency_key"],
        "material_refs": json.loads(row["material_refs_json"]),
        "payload": json.loads(row["payload_json"]),
        "note": row["note"],
        "reversal_of_seq": row["reversal_of_seq"],
        "occurred_at": row["occurred_at"],
    }


def replay(events: list[dict]) -> CaseState:
    """从（可能很长的）事件流重建当前状态；冲正事件会抵消原录入。"""
    state = CaseState(case_id=events[0]["case_id"] if events else "")
    for e in events:
        etype = e["event_type"]
        p = e["payload"]

        if etype == "entry_reversed":
            state.reversed_seqs.add(p["target_seq"])
            # 在投影中把被冲正的材料版本标记掉
            for versions in state.materials.values():
                for v in versions:
                    if v.event_seq == p["target_seq"]:
                        v.reversed = True
                        v.reversed_by_seq = e["seq"]
            # 阶段进入事件被冲正 → 退出对应阶段（frontier 规则保证无下游事件）
            state.stage_entries = {
                s: seq for s, seq in state.stage_entries.items()
                if seq != p["target_seq"]
            }
            # 若原鉴定被冲正，重算鉴定人集合与下游批准
            target = next(
                (x for x in events if x["seq"] == p["target_seq"]), None
            )
            if target and target["event_type"] == "attribution_recorded":
                state.experts.discard(target["actor_id"])
                state.approvals.pop("issue_request", None)
                state.links.pop("attribution", None)
            if target and target["event_type"] == "evidence_sealed":
                state.links.pop("evidence_seal", None)
            continue

        if e["seq"] in state.reversed_seqs:
            continue

        if etype == "case_opened":
            state.opened = True
            state.item = p["item"]
            state.team = p.get("team", {})
        elif etype == "material_registered":
            mv = MaterialVersion(
                material_id=p["material_id"],
                version=p["version"],
                kind=p["kind"],
                title=p["title"],
                sha256=p.get("sha256"),
                external_ref=p.get("external_ref"),
                registered_by=e["actor_id"],
                event_seq=e["seq"],
            )
            state.materials.setdefault(p["material_id"], []).append(mv)
        elif etype == "evidence_sealed":
            state.stage_entries[CaseStage.EVIDENCE_SEALED] = e["seq"]
            state.links["evidence_seal"] = {"seq": e["seq"], **p}
        elif etype == "attribution_recorded":
            state.stage_entries[CaseStage.ATTRIBUTION] = e["seq"]
            state.experts.add(e["actor_id"])
            state.links["attribution"] = {"seq": e["seq"], **p}
        elif etype == "request_issued":
            state.stage_entries[CaseStage.REQUESTED] = e["seq"]
            state.request_ref = p["external_ref"]
            state.links["external_request"] = {"seq": e["seq"], **p}
        elif etype == "counterpart_confirmed":
            state.confirmation_in_reply_to = p.get("in_reply_to")
            state.links["counterpart_confirmation"] = {"seq": e["seq"], **p}
        elif etype == "handover_completed":
            state.stage_entries[CaseStage.HANDOVER] = e["seq"]
            state.receipt_ref = p["external_ref"]
            state.links["handover_receipt"] = {"seq": e["seq"], **p}
        elif etype == "case_archived":
            state.stage_entries[CaseStage.ARCHIVED] = e["seq"]
            state.links["archive"] = {"seq": e["seq"], **p}
        elif etype == "approval_recorded":
            state.approvals[p["action"]] = {
                "seq": e["seq"],
                "approved": p["approved"],
                "actor_id": e["actor_id"],
                "note": p.get("note"),
                "material_refs": e["material_refs"],
                "decided_at": e["occurred_at"],
            }
        elif etype == "dispute_raised":
            state.disputed = True
            state.dispute_history.append({"seq": e["seq"], **p, "raised_at": e["occurred_at"]})
        elif etype == "dispute_resolved":
            state.disputed = False
            state.dispute_history[-1]["resolved_seq"] = e["seq"]
            state.dispute_history[-1]["resolved_at"] = e["occurred_at"]
            state.dispute_history[-1]["resolution"] = p.get("resolution")
    state.events = events
    # 当前阶段 = 最近一次（未被冲正的）阶段进入事件
    if state.stage_entries:
        state.stage = max(
            state.stage_entries, key=lambda s: STAGE_ORDER.index(CaseStage(s))
        )
    else:
        state.stage = CaseStage.INTAKE
    return state


# ---------------------------------------------------------------- 引擎


class CaseEngine:
    def __init__(
        self,
        store: EventStore,
        *,
        clock: Callable[[], str] = utc_now,
    ):
        self.store = store
        self.clock = clock
        self._users: dict[str, dict] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._load_users()

    # ---- 用户目录

    def _load_users(self) -> None:
        self.store.conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                   user_id TEXT PRIMARY KEY,
                   agency  TEXT NOT NULL,
                   roles_json TEXT NOT NULL,
                   created_at TEXT NOT NULL
               )"""
        )
        for row in self.store.conn.execute("SELECT * FROM users"):
            self._users[row["user_id"]] = {
                "user_id": row["user_id"],
                "agency": row["agency"],
                "roles": json.loads(row["roles_json"]),
                "created_at": row["created_at"],
            }

    def register_user(self, user_id: str, agency: str, roles: list[str]) -> dict:
        for r in roles:
            Role(r)  # 非法角色直接抛 ValueError
        if user_id in self._users:
            raise DomainError("用户已存在", code="user_exists")
        record = {
            "user_id": user_id,
            "agency": agency,
            "roles": list(roles),
            "created_at": self.clock(),
        }
        try:
            self.store.conn.execute(
                "INSERT INTO users(user_id, agency, roles_json, created_at) VALUES(?,?,?,?)",
                (user_id, agency, json.dumps(roles, ensure_ascii=False), record["created_at"]),
            )
        except sqlite3.IntegrityError as exc:
            raise DomainError("用户已存在", code="user_exists") from exc
        self._users[user_id] = record
        return record

    def _user(self, actor_id: str) -> dict:
        user = self._users.get(actor_id)
        if user is None:
            raise AuthzError(f"操作者 {actor_id} 未登记")
        return user

    def _require_role(self, actor_id: str, allowed: Role | set[Role]) -> dict:
        user = self._user(actor_id)
        allowed_set = {allowed} if isinstance(allowed, Role) else allowed
        if not (set(user["roles"]) & {r.value for r in allowed_set}):
            raise AuthzError(
                f"{actor_id} 的职责 {user['roles']} 不包含 "
                f"{sorted(r.value for r in allowed_set)}"
            )
        return user

    # ---- 事务骨架

    def _lock_for(self, case_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(case_id, threading.RLock())

    def _mutate(
        self, case_id: str, idem_key: str | None, fn: Callable[[CaseState], dict]
    ) -> dict:
        """在逐案件锁 + 立即写事务内：重放 → 决策 → 追加 → 提交。"""
        lock = self._lock_for(case_id)
        with lock:
            self.store.begin()
            try:
                rows = self.store.load_case(case_id)
                state = replay([_event_dict(r) for r in rows])
                if idem_key and rows:
                    for e in state.events:
                        if e["idempotency_key"] == idem_key:
                            self.store.rollback()
                            raise DuplicateEventError(
                                "相同幂等键的请求已处理",
                                original_seq=e["seq"],
                                replayed=True,
                            )
                result = fn(state)
                self.store.commit()
                return result
            except sqlite3.IntegrityError as exc:
                # 并发兜底：另一线程已用同一幂等键落库
                self.store.rollback()
                rows = self.store.load_case(case_id)
                for row in rows:
                    if row["idempotency_key"] == idem_key:
                        raise DuplicateEventError(
                            "相同幂等键的请求已处理",
                            original_seq=row["seq"],
                            replayed=True,
                        ) from exc
                raise DomainError(f"数据约束冲突: {exc}") from exc
            except sqlite3.OperationalError as exc:
                self.store.rollback()
                if "locked" in str(exc):
                    raise OrderingError(
                        "案件正被其他事务处理，请重试",
                    ) from exc
                raise
            except Exception:
                self.store.rollback()
                raise

    def _append(self, state: CaseState, **kw) -> dict:
        seq = self.store.append(case_id=state.case_id, occurred_at=self.clock(), **kw)
        return {"seq": seq}

    def _assert_open(self, state: CaseState) -> None:
        if not state.opened:
            raise NotFoundError(f"案件 {state.case_id} 不存在")

    def _assert_not_disputed(self, state: CaseState, action: str) -> None:
        if state.disputed:
            raise OrderingError(
                f"案件存在未决争议，物件处于受限状态，禁止“{action}”"
            )

    @staticmethod
    def _assert_stage(state: CaseState, expected: CaseState | CaseStage) -> None:
        need = expected if isinstance(expected, CaseStage) else expected.stage
        if state.stage != need:
            raise OrderingError(
                f"当前阶段为 {state.stage}，该操作要求处于 {need}"
            )

    def _find_ref_origin(
        self,
        state: CaseState,
        ref: str,
        event_types: tuple[str, ...] = (
            "request_issued",
            "counterpart_confirmed",
            "handover_completed",
        ),
    ) -> int | None:
        for e in state.events:
            if (
                e["event_type"] in event_types
                and e["payload"].get("external_ref") == ref
            ):
                return e["seq"]
        return None

    # ---- 命令

    def open_case(
        self,
        case_id: str | None,
        actor_id: str,
        item: dict[str, Any],
        team: dict[str, Any] | None = None,
        idem_key: str | None = None,
    ) -> dict:
        self._require_role(actor_id, Role.CASE_OFFICER)
        case_id = case_id or f"CASE-{uuid.uuid4().hex[:12].upper()}"
        validate_external_id(case_id)
        if not item.get("name"):
            raise DomainError("文物名称必填", code="item_name_required")
        team = team or {}
        for member_id in [
            *team.get("expert_ids", []),
            *team.get("reviewer_ids", []),
            *team.get("approver_ids", []),
            team.get("custodian_id"),
        ]:
            if member_id:
                self._user(member_id)

        lock = self._lock_for(case_id)
        with lock:
            self.store.begin()
            try:
                existing_rows = self.store.load_case(case_id)
                if existing_rows:
                    self.store.rollback()
                    first = existing_rows[0]
                    if idem_key and first["idempotency_key"] == idem_key:
                        raise DuplicateEventError(
                            f"案件 {case_id} 已存在",
                            original_seq=first["seq"],
                            replayed=True,
                        )
                    raise DuplicateEventError(
                        f"案件 {case_id} 已存在",
                        original_seq=first["seq"],
                    )
                seq = self.store.append(
                    case_id=case_id,
                    event_type="case_opened",
                    actor_id=actor_id,
                    actor_role=Role.CASE_OFFICER.value,
                    idempotency_key=idem_key,
                    payload={"item": item, "team": team},
                    occurred_at=self.clock(),
                )
                self.store.commit()
            except sqlite3.IntegrityError:
                self.store.rollback()
                rows = self.store.load_case(case_id)
                first = rows[0] if rows else None
                if first and idem_key and first["idempotency_key"] == idem_key:
                    raise DuplicateEventError(
                        f"案件 {case_id} 已存在",
                        original_seq=first["seq"],
                        replayed=True,
                    )
                raise DuplicateEventError(
                    f"案件 {case_id} 已存在",
                    original_seq=first["seq"] if first else 0,
                )
            except Exception:
                self.store.rollback()
                raise
        return {"case_id": case_id, "seq": seq}

    def register_material(
        self,
        case_id: str,
        actor_id: str,
        *,
        material_id: str,
        kind: str,
        title: str,
        version: int | None = None,
        sha256: str | None = None,
        external_ref: str | None = None,
        idem_key: str | None = None,
    ) -> dict:
        user = self._user(actor_id)
        kind = MaterialKind(kind)
        if not title:
            raise DomainError("材料标题必填")

        def cmd(state: CaseState) -> dict:
            self._assert_open(state)
            existing = state.materials.get(material_id, [])
            next_version = (max(v.version for v in existing) + 1) if existing else 1
            if version is not None and version != next_version:
                raise DomainError(
                    f"材料 {material_id} 下一版本应为 {next_version}",
                    code="version_conflict",
                )
            if external_ref:
                validate_external_id(external_ref)
                # 外部文号全局去重：先看已登记材料，再看阶段事件
                for versions in state.materials.values():
                    for v in versions:
                        if v.external_ref == external_ref and not v.reversed:
                            raise DuplicateEventError(
                                f"外部标识 {external_ref} 的文书已登记",
                                original_seq=v.event_seq,
                            )
                origin = self._find_ref_origin(state, external_ref)
                if origin is not None:
                    raise DuplicateEventError(
                        f"外部标识 {external_ref} 的函件/回执已登记",
                        original_seq=origin,
                    )
            return self._append(
                state,
                event_type="material_registered",
                actor_id=actor_id,
                actor_role=user["roles"][0],
                idempotency_key=idem_key,
                payload={
                    "material_id": material_id,
                    "version": next_version,
                    "kind": kind.value,
                    "title": title,
                    "sha256": sha256,
                    "external_ref": external_ref,
                },
            )

        return self._mutate(case_id, idem_key, cmd)

    def raise_dispute(self, case_id: str, actor_id: str, reason: str) -> dict:
        self._user(actor_id)
        if not reason:
            raise DomainError("争议理由必填")

        def cmd(state: CaseState) -> dict:
            self._assert_open(state)
            if state.stage == CaseStage.ARCHIVED:
                raise OrderingError("案件已结案归档，不能再提出争议")
            if state.disputed:
                raise OrderingError("案件已处于争议受限状态")
            return self._append(
                state,
                event_type="dispute_raised",
                actor_id=actor_id,
                actor_role=self._users[actor_id]["roles"][0],
                payload={"reason": reason},
            )

        return self._mutate(case_id, None, cmd)

    def resolve_dispute(
        self, case_id: str, actor_id: str, resolution: str
    ) -> dict:
        self._require_role(actor_id, {Role.REVIEWER, Role.CASE_OFFICER})

        def cmd(state: CaseState) -> dict:
            self._assert_open(state)
            if not state.disputed:
                raise OrderingError("案件当前没有未决争议")
            return self._append(
                state,
                event_type="dispute_resolved",
                actor_id=actor_id,
                actor_role=self._users[actor_id]["roles"][0],
                payload={"resolution": resolution},
            )

        return self._mutate(case_id, None, cmd)

    # ---- 阶段推进

    def seal_evidence(
        self,
        case_id: str,
        actor_id: str,
        material_refs: list[dict],
        *,
        seal_id: str,
        idem_key: str | None = None,
    ) -> dict:
        self._require_role(actor_id, Role.CASE_OFFICER)

        def cmd(state: CaseState) -> dict:
            self._assert_open(state)
            self._assert_not_disputed(state, "证据封存")
            self._assert_stage(state, CaseStage.INTAKE)
            snapshot = state.refs_for(material_refs)
            if not any(r["kind"] == MaterialKind.SEIZURE_RECORD for r in snapshot):
                raise DomainError(
                    "封存必须附至少一份查获记录（seizure_record）",
                    code="missing_seizure_record",
                )
            return self._append(
                state,
                event_type="evidence_sealed",
                actor_id=actor_id,
                actor_role=Role.CASE_OFFICER.value,
                idempotency_key=idem_key,
                material_refs=snapshot,
                payload={"seal_id": seal_id},
                note="证据封存：材料版本随决定固化",
            )

        return self._mutate(case_id, idem_key, cmd)

    def record_attribution(
        self,
        case_id: str,
        actor_id: str,
        material_refs: list[dict],
        *,
        conclusion: str,
        idem_key: str | None = None,
    ) -> dict:
        self._require_role(actor_id, Role.EXPERT)

        def cmd(state: CaseState) -> dict:
            self._assert_open(state)
            self._assert_not_disputed(state, "专家鉴定")
            self._assert_stage(state, CaseStage.EVIDENCE_SEALED)
            snapshot = state.refs_for(material_refs)
            if not any(r["kind"] == MaterialKind.EXPERT_OPINION for r in snapshot):
                raise DomainError(
                    "鉴定结论必须附专家意见书（expert_opinion）",
                    code="missing_expert_opinion",
                )
            return self._append(
                state,
                event_type="attribution_recorded",
                actor_id=actor_id,
                actor_role=Role.EXPERT.value,
                idempotency_key=idem_key,
                material_refs=snapshot,
                payload={"conclusion": conclusion},
            )

        return self._mutate(case_id, idem_key, cmd)

    def record_approval(
        self,
        case_id: str,
        actor_id: str,
        *,
        action: str,
        approved: bool,
        note: str | None = None,
        material_refs: list[dict] | None = None,
        idem_key: str | None = None,
    ) -> dict:
        if action not in APPROVAL_RULES:
            raise DomainError(f"未知关键动作 {action}")
        self._require_role(actor_id, APPROVAL_RULES[action])

        def cmd(state: CaseState) -> dict:
            self._assert_open(state)
            self._assert_not_disputed(state, f"批准 {action}")
            self._assert_stage(state, APPROVAL_STAGE[action])
            if actor_id in state.experts:
                raise AuthzError(
                    f"{actor_id} 参与过本案原鉴定，不得复核或批准该关键动作"
                )
            snapshot = state.refs_for(material_refs or [])
            # 并发下重复批准：同一动作已有批准决定即冲突，不能叠加
            prior = state.approvals.get(action)
            if prior and prior["approved"] and approved:
                raise DuplicateEventError(
                    f"动作 {action} 已由 {prior['actor_id']} 批准",
                    original_seq=prior["seq"],
                )
            return self._append(
                state,
                event_type="approval_recorded",
                actor_id=actor_id,
                actor_role=next(
                    r
                    for r in self._users[actor_id]["roles"]
                    if Role(r) in APPROVAL_RULES[action]
                ),
                idempotency_key=idem_key,
                material_refs=snapshot,
                payload={"action": action, "approved": approved, "note": note},
            )

        return self._mutate(case_id, idem_key, cmd)

    def issue_request(
        self,
        case_id: str,
        actor_id: str,
        material_refs: list[dict],
        *,
        external_ref: str,
        idem_key: str | None = None,
    ) -> dict:
        self._require_role(actor_id, Role.CASE_OFFICER)
        validate_external_id(external_ref)

        def cmd(state: CaseState) -> dict:
            self._assert_open(state)
            self._assert_not_disputed(state, "对外请求")
            # 已处理过的同文号函件，任何阶段重送都按重复处理
            origin = self._find_ref_origin(state, external_ref)
            if origin is not None:
                raise DuplicateEventError(
                    f"对外请求函 {external_ref} 已登记", original_seq=origin
                )
            self._assert_stage(state, CaseStage.ATTRIBUTION)
            approval = state.approvals.get("issue_request")
            if not approval or not approval["approved"]:
                raise OrderingError("对外请求须先经未参与鉴定的批准人批准")
            snapshot = state.refs_for(material_refs)
            if not any(r["kind"] == MaterialKind.DIPLOMATIC_NOTE for r in snapshot):
                raise DomainError(
                    "对外请求须附外交函件（diplomatic_note）",
                    code="missing_diplomatic_note",
                )
            return self._append(
                state,
                event_type="request_issued",
                actor_id=actor_id,
                actor_role=Role.CASE_OFFICER.value,
                idempotency_key=idem_key,
                material_refs=snapshot,
                payload={
                    "external_ref": external_ref,
                    "approved_by": approval["actor_id"],
                    "approval_seq": approval["seq"],
                },
            )

        return self._mutate(case_id, idem_key, cmd)

    def record_confirmation(
        self,
        case_id: str,
        actor_id: str,
        material_refs: list[dict],
        *,
        external_ref: str,
        in_reply_to: str,
        idem_key: str | None = None,
    ) -> dict:
        """境外对方确认函重复送达：同一 external_ref 绝不二次推进。"""
        self._require_role(actor_id, {Role.CASE_OFFICER, Role.CUSTODIAN})
        validate_external_id(external_ref)
        validate_external_id(in_reply_to)

        def cmd(state: CaseState) -> dict:
            self._assert_open(state)
            self._assert_not_disputed(state, "对方确认登记")
            # 重复确认函即使在阶段过后迟到送达，也必须识别为重复
            origin = self._find_ref_origin(state, external_ref)
            if origin is not None:
                raise DuplicateEventError(
                    f"对方确认函 {external_ref} 已送达并登记",
                    original_seq=origin,
                )
            self._assert_stage(state, CaseStage.REQUESTED)
            if state.request_ref != in_reply_to:
                raise DomainError(
                    f"确认函应回复我方请求 {state.request_ref}，实际为 {in_reply_to}",
                    code="ref_mismatch",
                )
            snapshot = state.refs_for(material_refs)
            if not any(r["kind"] == MaterialKind.DIPLOMATIC_NOTE for r in snapshot):
                raise DomainError("对方确认须附外交函件（diplomatic_note）")
            return self._append(
                state,
                event_type="counterpart_confirmed",
                actor_id=actor_id,
                actor_role=self._users[actor_id]["roles"][0],
                idempotency_key=idem_key,
                material_refs=snapshot,
                payload={"external_ref": external_ref, "in_reply_to": in_reply_to},
            )

        return self._mutate(case_id, idem_key, cmd)

    def complete_handover(
        self,
        case_id: str,
        actor_id: str,
        material_refs: list[dict],
        *,
        external_ref: str,
        idem_key: str | None = None,
    ) -> dict:
        self._require_role(actor_id, Role.CUSTODIAN)
        validate_external_id(external_ref)

        def cmd(state: CaseState) -> dict:
            self._assert_open(state)
            self._assert_not_disputed(state, "运输交接")
            origin = self._find_ref_origin(state, external_ref)
            if origin is not None:
                raise DuplicateEventError(
                    f"交接回执 {external_ref} 已登记", original_seq=origin
                )
            self._assert_stage(state, CaseStage.REQUESTED)
            if "counterpart_confirmation" not in state.links:
                raise OrderingError("交接前必须先收到境外对方确认函")
            approval = state.approvals.get("complete_handover")
            if not approval or not approval["approved"]:
                raise OrderingError("实体交接须先经具备资格者复核批准")
            snapshot = state.refs_for(material_refs)
            if not any(r["kind"] == MaterialKind.HANDOVER_RECEIPT for r in snapshot):
                raise DomainError(
                    "交接须附交接回执（handover_receipt）",
                    code="missing_receipt",
                )
            return self._append(
                state,
                event_type="handover_completed",
                actor_id=actor_id,
                actor_role=Role.CUSTODIAN.value,
                idempotency_key=idem_key,
                material_refs=snapshot,
                payload={
                    "external_ref": external_ref,
                    "custodian_id": actor_id,
                    "approved_by": approval["actor_id"],
                    "approval_seq": approval["seq"],
                },
            )

        return self._mutate(case_id, idem_key, cmd)

    def archive_case(
        self,
        case_id: str,
        actor_id: str,
        *,
        archive_location: str,
        idem_key: str | None = None,
    ) -> dict:
        self._require_role(actor_id, Role.CASE_OFFICER)

        def cmd(state: CaseState) -> dict:
            self._assert_open(state)
            self._assert_not_disputed(state, "结案归档")
            self._assert_stage(state, CaseStage.HANDOVER)
            approval = state.approvals.get("archive")
            if not approval or not approval["approved"]:
                raise OrderingError("结案归档须先经复核批准")
            return self._append(
                state,
                event_type="case_archived",
                actor_id=actor_id,
                actor_role=Role.CASE_OFFICER.value,
                idempotency_key=idem_key,
                payload={"archive_location": archive_location},
            )

        return self._mutate(case_id, idem_key, cmd)

    # ---- 撤销（冲正，不抹历史）

    def reverse_entry(
        self, case_id: str, actor_id: str, target_seq: int, reason: str
    ) -> dict:
        self._require_role(actor_id, {Role.CASE_OFFICER, Role.REVIEWER})
        if not reason:
            raise DomainError("撤销理由必填")

        def cmd(state: CaseState) -> dict:
            self._assert_open(state)
            self._assert_not_disputed(state, "冲正撤销")
            target = next(
                (e for e in state.events if e["seq"] == target_seq), None
            )
            if target is None:
                raise NotFoundError(f"事件 {target_seq} 不存在")
            if target["event_type"] not in REVERSIBLE_TYPES:
                raise DomainError(
                    f"事件类型 {target['event_type']} 不允许撤销",
                    code="not_reversible",
                )
            if target_seq in state.reversed_seqs:
                raise DomainError("该事件已被撤销", code="already_reversed")
            # 只能撤销当前阶段前沿的录入，已越过的阶段不可倒改
            entered_stage = {
                "evidence_sealed": CaseStage.EVIDENCE_SEALED,
                "attribution_recorded": CaseStage.ATTRIBUTION,
            }.get(target["event_type"])
            if entered_stage is not None and state.stage != entered_stage:
                raise OrderingError("流程已越过该节点，不能撤销，只能另作更正记录")
            # 被后续决定引用的材料版本不得撤销
            for e in state.events:
                if e["seq"] <= target_seq:
                    continue
                for ref in e.get("material_refs", []):
                    if ref.get("event_seq") == target_seq:
                        raise OrderingError(
                            f"该录入已被事件 {e['seq']} 的决定引用，不能撤销"
                        )
            return self._append(
                state,
                event_type="entry_reversed",
                actor_id=actor_id,
                actor_role=self._users[actor_id]["roles"][0],
                reversal_of_seq=target_seq,
                payload={"target_seq": target_seq, "reason": reason},
                note="冲正录入；原事件保留在监管台账中",
            )

        return self._mutate(case_id, None, cmd)

    # ---- 读模型

    def get_state(self, case_id: str) -> CaseState:
        rows = self.store.load_case(case_id)
        if not rows:
            raise NotFoundError(f"案件 {case_id} 不存在")
        return replay([_event_dict(r) for r in rows])

    def list_cases(self) -> list[str]:
        return self.store.list_cases()

    # ---- 条件/责任人/链路视图

    def case_view(self, case_id: str) -> dict:
        state = self.get_state(case_id)
        return build_case_view(state, self._users)


def build_case_view(state: CaseState, users: dict[str, dict]) -> dict:
    """生成工作人员可直接判读的视图：谁负责、缺什么、链路是否闭合。"""
    team = state.team

    def names(ids):
        return [
            {"user_id": i, "agency": users.get(i, {}).get("agency")}
            for i in ids or []
            if i
        ]

    responsible: dict[str, Any] = {}
    conditions: list[dict] = []

    def cond(code: str, satisfied: bool, detail: str, needed_role: str | None = None):
        conditions.append(
            {
                "code": code,
                "satisfied": satisfied,
                "detail": detail,
                "needed_role": needed_role,
            }
        )

    stage = state.stage
    if stage == CaseStage.INTAKE:
        responsible = {"role": Role.CASE_OFFICER.value,
                       "members": names([team.get("case_officer_id")])}
        cond("seizure_record", state.has_kind(MaterialKind.SEIZURE_RECORD),
             "登记查获记录材料", Role.CASE_OFFICER.value)
        cond("seal_evidence", "evidence_seal" in state.links,
             "由办案员封存证据", Role.CASE_OFFICER.value)
    elif stage == CaseStage.EVIDENCE_SEALED:
        responsible = {"role": Role.EXPERT.value,
                       "members": names(team.get("expert_ids"))}
        cond("expert_opinion", state.has_kind(MaterialKind.EXPERT_OPINION),
             "登记专家意见书", Role.EXPERT.value)
        cond("attribution", "attribution" in state.links,
             "专家录入鉴定结论", Role.EXPERT.value)
    elif stage == CaseStage.ATTRIBUTION:
        ap = state.approvals.get("issue_request")
        responsible = {"role": Role.APPROVER.value,
                       "members": names(team.get("approver_ids"))}
        cond("request_approval", bool(ap and ap["approved"]),
             "由未参与鉴定的批准人批准对外请求", Role.APPROVER.value)
        cond("diplomatic_note", state.has_kind(MaterialKind.DIPLOMATIC_NOTE),
             "拟定并登记对外外交函件", Role.CASE_OFFICER.value)
        cond("request_issued", "external_request" in state.links,
             "正式对外发出请求", Role.CASE_OFFICER.value)
    elif stage == CaseStage.REQUESTED:
        confirmed = "counterpart_confirmation" in state.links
        ap = state.approvals.get("complete_handover")
        if not confirmed:
            responsible = {"role": "counterpart_authority",
                           "members": [],
                           "note": "等待境外执法机关回函确认"}
        else:
            responsible = {"role": Role.CUSTODIAN.value,
                           "members": names([team.get("custodian_id")])}
        cond("counterpart_confirmation", confirmed,
             "收到境外对方确认函（函件需引用我方请求编号）",
             "counterpart_authority")
        cond("handover_approval", bool(ap and ap["approved"]),
             "由未参与鉴定的复核/批准人批准实体交接", Role.REVIEWER.value)
        cond("handover_receipt_material",
             state.has_kind(MaterialKind.HANDOVER_RECEIPT),
             "取得交接回执材料", Role.CUSTODIAN.value)
        cond("handover_completed", "handover_receipt" in state.links,
             "运输交接并登记回执", Role.CUSTODIAN.value)
    elif stage == CaseStage.HANDOVER:
        ap = state.approvals.get("archive")
        responsible = {"role": Role.REVIEWER.value,
                       "members": names(team.get("reviewer_ids"))}
        cond("archive_approval", bool(ap and ap["approved"]),
             "复核批准结案归档", Role.REVIEWER.value)
        cond("archived", "archive" in state.links,
             "办案员归档结案", Role.CASE_OFFICER.value)
    else:
        responsible = {"role": None, "members": [], "note": "案件已归档闭合"}

    chain = [
        ("evidence_seal", "证据封存"),
        ("attribution", "专家鉴定"),
        ("external_request", "对外请求"),
        ("counterpart_confirmation", "对方确认"),
        ("handover_receipt", "交接回执"),
        ("archive", "结案归档"),
    ]
    chain_links = []
    for key, label in chain:
        link = state.links.get(key)
        chain_links.append(
            {
                "link": key,
                "label": label,
                "present": link is not None,
                "event_seq": link.get("seq") if link else None,
            }
        )
    refs_match = (
        state.request_ref is not None
        and state.confirmation_in_reply_to == state.request_ref
    )
    chain_complete = (
        stage == CaseStage.ARCHIVED
        and all(l["present"] for l in chain_links)
        and refs_match
    )

    materials_out = []
    for mid, versions in sorted(state.materials.items()):
        materials_out.append(
            {
                "material_id": mid,
                "current_version": next(
                    (v.version for v in reversed(versions) if not v.reversed), None
                ),
                "versions": [
                    {
                        "version": v.version,
                        "kind": v.kind,
                        "title": v.title,
                        "sha256": v.sha256,
                        "external_ref": v.external_ref,
                        "registered_by": v.registered_by,
                        "event_seq": v.event_seq,
                        "reversed": v.reversed,
                        "reversed_by_event": v.reversed_by_seq,
                    }
                    for v in versions
                ],
            }
        )

    return {
        "case_id": state.case_id,
        "item": state.item,
        "stage": stage,
        "disputed": state.disputed,
        "restricted": state.disputed,
        "responsible": responsible,
        "missing_conditions": [c for c in conditions if not c["satisfied"]],
        "conditions": conditions,
        "team": team,
        "experts": sorted(state.experts),
        "materials": materials_out,
        "approvals": state.approvals,
        "dispute_history": state.dispute_history,
        "chain": {
            "refs_match": refs_match,
            "complete": chain_complete,
            "links": chain_links,
        },
    }


def build_ledger(state: CaseState, users: dict[str, dict]) -> dict:
    """按案件导出的完整监管记录：含全部历史事件（含已撤销者）。"""
    view = build_case_view(state, users)
    decisions = []
    for e in state.events:
        if e["event_type"] in {
            "evidence_sealed",
            "attribution_recorded",
            "request_issued",
            "counterpart_confirmed",
            "handover_completed",
            "case_archived",
            "approval_recorded",
        }:
            decisions.append(
                {
                    "seq": e["seq"],
                    "decision": e["event_type"],
                    "actor_id": e["actor_id"],
                    "actor_role": e["actor_role"],
                    "decided_at": e["occurred_at"],
                    "material_basis": e["material_refs"],
                    "payload": e["payload"],
                    "reversed": e["seq"] in state.reversed_seqs,
                }
            )
    return {
        **view,
        "decisions": decisions,
        "event_log": [
            {
                "seq": e["seq"],
                "event_type": e["event_type"],
                "actor_id": e["actor_id"],
                "actor_role": e["actor_role"],
                "occurred_at": e["occurred_at"],
                "payload": e["payload"],
                "material_refs": e["material_refs"],
                "idempotency_key": e["idempotency_key"],
                "reversal_of_seq": e["reversal_of_seq"],
                "note": e["note"],
            }
            for e in state.events
        ],
    }
