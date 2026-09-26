"""重复送达：外部函件/回执二次到达与幂等重试都不得重复推进。"""

import unittest

from src.relic_case.engine import DuplicateEventError
from support import build_engine, drive_to


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        self.engine = build_engine()

    def test_duplicate_confirmation_external_ref_does_not_advance(self):
        cid = "CASE-2026-0001"
        drive_to(self.engine, "requested")
        self.engine.register_material(
            cid, "custodian", material_id="M-IN",
            kind="diplomatic_note", title="确认函", external_ref="NOTE-IN-0001",
        )
        self.engine.record_confirmation(
            cid, "custodian", [{"material_id": "M-IN"}],
            external_ref="NOTE-IN-0001", in_reply_to="NOTE-OUT-0001",
        )

        # 第一道防线：同文号函件换材料条目再次登记 → 拒绝
        with self.assertRaises(DuplicateEventError):
            self.engine.register_material(
                cid, "custodian", material_id="M-IN-DUP",
                kind="diplomatic_note", title="确认函（重发）",
                external_ref="NOTE-IN-0001",
            )

        # 第二道防线：即便直接重放确认动作（相同文号）→ 同样拒绝
        with self.assertRaises(DuplicateEventError) as ctx:
            self.engine.record_confirmation(
                cid, "custodian", [{"material_id": "M-IN"}],
                external_ref="NOTE-IN-0001",
                in_reply_to="NOTE-OUT-0001",
            )
        self.assertEqual(ctx.exception.code, "duplicate_event")

        # 确认事件只有一条，流程未重复推进
        state = self.engine.get_state(cid)
        confirmations = [
            e for e in state.events if e["event_type"] == "counterpart_confirmed"
        ]
        self.assertEqual(len(confirmations), 1)
        self.assertEqual(state.stage, "requested")

    def test_duplicate_handover_receipt_does_not_advance(self):
        drive_to(self.engine, "requested")
        self.engine.register_material(
            "CASE-2026-0001", "custodian", material_id="M-IN",
            kind="diplomatic_note", title="确认", external_ref="NOTE-IN-0001",
        )
        self.engine.record_confirmation(
            "CASE-2026-0001", "custodian", [{"material_id": "M-IN"}],
            external_ref="NOTE-IN-0001", in_reply_to="NOTE-OUT-0001",
        )
        self.engine.record_approval(
            "CASE-2026-0001", "reviewer", action="complete_handover",
            approved=True,
        )
        self.engine.register_material(
            "CASE-2026-0001", "custodian", material_id="M-RCPT",
            kind="handover_receipt", title="回执", external_ref="RCPT-0001",
        )
        self.engine.complete_handover(
            "CASE-2026-0001", "custodian", [{"material_id": "M-RCPT"}],
            external_ref="RCPT-0001",
        )
        with self.assertRaises(DuplicateEventError):
            self.engine.complete_handover(
                "CASE-2026-0001", "custodian", [{"material_id": "M-RCPT"}],
                external_ref="RCPT-0001",
            )
        self.assertEqual(
            self.engine.get_state("CASE-2026-0001").stage, "handover"
        )

    def test_idempotency_key_replay_does_not_double_append(self):
        cid = "CASE-2026-0099"
        self.engine.open_case(
            cid, "officer", {"name": "测试件"}, idem_key="IDEM-CASE-0001",
        )
        # 网络重试：同键重放，不新增事件
        with self.assertRaises(DuplicateEventError) as ctx:
            self.engine.open_case(
                cid, "officer", {"name": "测试件"}, idem_key="IDEM-CASE-0001",
            )
        self.assertTrue(ctx.exception.replayed)

        self.engine.register_material(
            cid, "officer", material_id="M", kind="seizure_record",
            title="记录", idem_key="IDEM-MAT-0001",
        )
        with self.assertRaises(DuplicateEventError) as ctx:
            self.engine.register_material(
                cid, "officer", material_id="M", kind="seizure_record",
                title="记录", idem_key="IDEM-MAT-0001",
            )
        self.assertTrue(ctx.exception.replayed)
        versions = self.engine.get_state(cid).materials["M"]
        self.assertEqual(len(versions), 1)

    def test_same_idempotency_key_rejected_for_different_intent(self):
        # 同键不能用于另一种动作
        cid = "CASE-2026-0100"
        self.engine.open_case(cid, "officer", {"name": "件"})
        self.engine.register_material(
            cid, "officer", material_id="M1", kind="seizure_record",
            title="记录1", idem_key="SHARED-KEY-0001",
        )
        with self.assertRaises(DuplicateEventError):
            self.engine.register_material(
                cid, "officer", material_id="M2", kind="ownership_claim",
                title="另一意图", idem_key="SHARED-KEY-0001",
            )

    def test_material_version_increments_deterministically(self):
        cid = "CASE-2026-0101"
        self.engine.open_case(cid, "officer", {"name": "件"})
        self.engine.register_material(
            cid, "officer", material_id="M", kind="seizure_record",
            title="v1",
        )
        self.engine.register_material(
            cid, "officer", material_id="M", kind="seizure_record",
            title="v2",
        )
        from src.relic_case.engine import DomainError

        with self.assertRaises(DomainError) as ctx:
            self.engine.register_material(
                cid, "officer", material_id="M", kind="seizure_record",
                title="bad", version=9,
            )
        self.assertEqual(ctx.exception.code, "version_conflict")
        self.assertEqual(
            [v.version for v in self.engine.get_state(cid).materials["M"]],
            [1, 2],
        )


if __name__ == "__main__":
    unittest.main()
