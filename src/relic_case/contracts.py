"""跨机构案件交换时共同使用的稳定标识。"""

from enum import StrEnum
import re


class CaseStage(StrEnum):
    """案件在协作链中的业务阶段。"""

    INTAKE = "intake"
    EVIDENCE_SEALED = "evidence_sealed"
    ATTRIBUTION = "attribution"
    REQUESTED = "requested"
    CONFIRMED = "confirmed"
    HANDOVER = "handover"
    ARCHIVED = "archived"


class MaterialKind(StrEnum):
    """能够作为决定依据的材料类别。"""

    SEIZURE_RECORD = "seizure_record"
    EXPERT_OPINION = "expert_opinion"
    OWNERSHIP_CLAIM = "ownership_claim"
    DIPLOMATIC_NOTE = "diplomatic_note"
    HANDOVER_RECEIPT = "handover_receipt"


class Role(StrEnum):
    """跨机构协作中的职责名称。"""

    CASE_OFFICER = "case_officer"
    EXPERT = "expert"
    REVIEWER = "reviewer"
    APPROVER = "approver"
    CUSTODIAN = "custodian"


class Action(StrEnum):
    """各机构系统可对案件触发的动作名称。"""

    SEAL_EVIDENCE = "seal_evidence"
    SUBMIT_APPRAISAL = "submit_appraisal"
    APPROVE_ATTRIBUTION = "approve_attribution"
    REJECT_ATTRIBUTION = "reject_attribution"
    DRAFT_REQUEST = "draft_request"
    APPROVE_REQUEST = "approve_request"
    RECORD_CONFIRMATION = "record_confirmation"
    RECORD_HANDOVER = "record_handover"
    CLOSE_CASE = "close_case"
    RAISE_DISPUTE = "raise_dispute"
    RESOLVE_DISPUTE = "resolve_dispute"


def validate_external_id(value: str) -> str:
    """校验外部系统提供的幂等标识。"""

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", value):
        raise ValueError("外部标识格式不正确")
    return value
