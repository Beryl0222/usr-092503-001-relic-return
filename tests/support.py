"""测试公用构造：标准五人团队与案件推进助手。"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.relic_case.engine import CaseEngine  # noqa: E402
from src.relic_case.storage import EventStore  # noqa: E402

USERS = {
    "officer": ("海关缉私局", ["case_officer"]),
    "expert": ("博物院鉴定部", ["expert"]),
    "expert_boss": ("博物院鉴定部", ["expert", "approver"]),
    "approver": ("文物外联局", ["approver"]),
    "reviewer": ("督查法务司", ["reviewer"]),
    "custodian": ("国家文物库房", ["custodian"]),
}

TEAM = {
    "case_officer_id": "officer",
    "expert_ids": ["expert"],
    "approver_ids": ["approver"],
    "reviewer_ids": ["reviewer"],
    "custodian_id": "custodian",
}


def build_engine(db_path: str = ":memory:") -> CaseEngine:
    engine = CaseEngine(EventStore(db_path))
    for uid, (agency, roles) in USERS.items():
        engine.register_user(uid, agency, roles)
    return engine


def open_case(engine: CaseEngine, case_id: str = "CASE-2026-0001") -> str:
    engine.open_case(
        case_id,
        "officer",
        {"name": "春秋青铜鼎", "category": "青铜器"},
        team=TEAM,
    )
    return case_id


def drive_to(engine: CaseEngine, stage: str, case_id: str = "CASE-2026-0001") -> None:
    """把案件推进到指定阶段（不含该阶段后的批准），供各测试选取入口。"""
    if not engine.store.load_case(case_id):
        open_case(engine, case_id)

    if stage == "intake":
        return

    engine.register_material(
        case_id, "officer", material_id="M-SEIZE", kind="seizure_record",
        title="境外查获记录", sha256="a" * 64,
    )
    engine.seal_evidence(case_id, "officer", [{"material_id": "M-SEIZE"}],
                         seal_id="SEAL-1")
    if stage == "evidence_sealed":
        return

    engine.register_material(
        case_id, "expert", material_id="M-OPINION", kind="expert_opinion",
        title="专家意见书 v1", sha256="b" * 64,
    )
    engine.record_attribution(case_id, "expert", [{"material_id": "M-OPINION"}],
                              conclusion="春秋时期青铜鼎，属流失文物")
    if stage == "attribution":
        return

    engine.record_approval(case_id, "approver", action="issue_request",
                           approved=True, note="同意对外请求")
    engine.register_material(
        case_id, "officer", material_id="M-NOTE-OUT", kind="diplomatic_note",
        title="请求返还外交函件", external_ref="NOTE-OUT-0001",
    )
    engine.issue_request(case_id, "officer", [{"material_id": "M-NOTE-OUT"}],
                         external_ref="NOTE-OUT-0001")
    if stage == "requested":
        return

    engine.register_material(
        case_id, "custodian", material_id="M-NOTE-IN", kind="diplomatic_note",
        title="对方确认函", external_ref="NOTE-IN-0001",
    )
    engine.record_confirmation(
        case_id, "custodian", [{"material_id": "M-NOTE-IN"}],
        external_ref="NOTE-IN-0001", in_reply_to="NOTE-OUT-0001",
    )
    engine.record_approval(case_id, "reviewer", action="complete_handover",
                           approved=True)
    engine.register_material(
        case_id, "custodian", material_id="M-RECEIPT", kind="handover_receipt",
        title="实体交接回执", external_ref="RECEIPT-0001",
    )
    engine.complete_handover(case_id, "custodian", [{"material_id": "M-RECEIPT"}],
                             external_ref="RECEIPT-0001")
    if stage == "handover":
        return

    engine.record_approval(case_id, "approver", action="archive", approved=True)
    engine.archive_case(case_id, "officer", archive_location="一号特藏库")
    if stage == "archived":
        return

    raise ValueError(f"未知阶段 {stage}")


def temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # 由 EventStore 自行创建
    return path
