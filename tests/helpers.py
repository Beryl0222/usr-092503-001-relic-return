"""测试共用的搭建工具：操作者名册与各阶段的流程驱动。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.relic_case import CaseService, EventStore  # noqa: E402

ACTORS = {
    "officer-1": {"name": "办案人甲", "org": "联合工作组", "roles": ["case_officer"]},
    "officer-2": {"name": "办案人乙", "org": "联合工作组", "roles": ["case_officer"]},
    "expert-1": {"name": "专家甲", "org": "鉴定中心", "roles": ["expert"]},
    "expert-2": {
        "name": "专家乙",
        "org": "鉴定中心",
        "roles": ["expert", "reviewer"],
    },
    "reviewer-1": {"name": "复核人甲", "org": "监管局", "roles": ["reviewer"]},
    "approver-1": {"name": "批准人甲", "org": "监管局", "roles": ["approver"]},
    "approver-2": {
        "name": "批准人乙",
        "org": "监管局",
        "roles": ["approver", "expert"],
    },
    "custodian-1": {"name": "保管人甲", "org": "博物馆", "roles": ["custodian"]},
}


def make_service(db_path=":memory:"):
    """建立服务并登记全部测试操作者。"""

    service = CaseService(EventStore(db_path))
    for actor_id, info in ACTORS.items():
        service.register_actor(actor_id, info["name"], info["org"], info["roles"])
    return service


def make_case(service, actor="officer-1", case_id=None):
    return service.create_case(
        actor, "西周青铜鼎返还案", {"name": "青铜鼎", "serial": "QD-001"}, case_id
    )


# ----------------------------------------------------------------------
# 单步推进：每个函数只完成本阶段的动作；suffix 用于多案件场景区分外部标识
# ----------------------------------------------------------------------


def step_seal(service, case_id, suffix=""):
    service.submit_material(
        "officer-1",
        case_id,
        "seizure_record",
        "海关查扣记录",
        external_id=f"SEIZE-2026-0001{suffix}",
    )
    service.perform("officer-1", case_id, "seal_evidence")


def step_appraise(service, case_id, suffix=""):
    service.perform(
        "expert-1", case_id, "submit_appraisal", {"conclusion": "确认为西周青铜器"}
    )
    service.perform("reviewer-1", case_id, "approve_attribution")


def step_request(service, case_id, suffix=""):
    service.perform(
        "officer-1",
        case_id,
        "draft_request",
        {"summary": "返还请求函", "external_id": f"NOTE-OUT-2026-0001{suffix}"},
    )
    service.perform("approver-1", case_id, "approve_request")


def step_confirm(service, case_id, suffix=""):
    service.perform(
        "officer-1",
        case_id,
        "record_confirmation",
        {"external_id": f"NOTE-IN-2026-0001{suffix}", "summary": "对方确认函"},
    )


def step_handover(service, case_id, suffix=""):
    service.perform(
        "custodian-1",
        case_id,
        "record_handover",
        {"external_id": f"RCPT-2026-0001{suffix}", "summary": "机场交接回执"},
    )


def step_close(service, case_id, suffix=""):
    service.perform("approver-1", case_id, "close_case")


# ----------------------------------------------------------------------
# 组合驱动：从新建案件一路推进到指定阶段
# ----------------------------------------------------------------------


def drive_to_evidence_sealed(service, case_id, suffix=""):
    step_seal(service, case_id, suffix)


def drive_to_attribution(service, case_id, suffix=""):
    drive_to_evidence_sealed(service, case_id, suffix)
    step_appraise(service, case_id, suffix)


def drive_to_requested(service, case_id, suffix=""):
    drive_to_attribution(service, case_id, suffix)
    step_request(service, case_id, suffix)


def drive_to_confirmed(service, case_id, suffix=""):
    drive_to_requested(service, case_id, suffix)
    step_confirm(service, case_id, suffix)


def drive_to_handover(service, case_id, suffix=""):
    drive_to_confirmed(service, case_id, suffix)
    step_handover(service, case_id, suffix)


def drive_to_archived(service, case_id, suffix=""):
    drive_to_handover(service, case_id, suffix)
    step_close(service, case_id, suffix)
