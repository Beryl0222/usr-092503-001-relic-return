"""案件事件：台账的唯一事实来源，服务重启后据此恢复。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class EventType(StrEnum):
    """事件类型。录入类事件可被撤销，决定类事件只增不改。"""

    ACTOR_REGISTERED = "actor_registered"
    CASE_CREATED = "case_created"
    MATERIAL_SUBMITTED = "material_submitted"
    EVIDENCE_SEALED = "evidence_sealed"
    APPRAISAL_SUBMITTED = "appraisal_submitted"
    ATTRIBUTION_APPROVED = "attribution_approved"
    ATTRIBUTION_REJECTED = "attribution_rejected"
    REQUEST_DRAFTED = "request_drafted"
    REQUEST_APPROVED = "request_approved"
    CONFIRMATION_RECORDED = "confirmation_recorded"
    HANDOVER_RECORDED = "handover_recorded"
    CASE_CLOSED = "case_closed"
    DISPUTE_RAISED = "dispute_raised"
    DISPUTE_RESOLVED = "dispute_resolved"
    EVENT_VOIDED = "event_voided"


@dataclass
class Event:
    """一条不可变的台账记录。"""

    event_id: str
    case_id: str
    type: EventType
    actor_id: str
    role: str | None
    occurred_at: str
    payload: dict = field(default_factory=dict)
    seq: int = 0
    voided_by: str | None = None

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "case_id": self.case_id,
            "type": self.type.value,
            "actor_id": self.actor_id,
            "role": self.role,
            "occurred_at": self.occurred_at,
            "payload": self.payload,
            "voided_by": self.voided_by,
        }
