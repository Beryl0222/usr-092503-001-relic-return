"""争议冻结：争议中的物件只能停留在受限状态，一切推进被禁止。"""

import unittest

from src.relic_case.engine import OrderingError
from support import build_engine, drive_to


class DisputeTests(unittest.TestCase):
    def setUp(self):
        self.engine = build_engine()

    def test_dispute_freezes_every_advancing_action(self):
        cid = "CASE-2026-0001"
        drive_to(self.engine, "attribution")
        self.engine.raise_dispute(cid, "expert", reason="权属主张存疑")
        state = self.engine.get_state(cid)
        self.assertTrue(state.disputed)

        with self.assertRaises(OrderingError):
            self.engine.record_approval(
                cid, "approver", action="issue_request", approved=True
            )
        # 争议期间对外发函推进也应拒绝
        self.engine.register_material(
            cid, "officer", material_id="M-NOTE", kind="diplomatic_note",
            title="函", external_ref="NOTE-FROZEN-1",
        )
        with self.assertRaises(OrderingError):
            self.engine.issue_request(
                cid, "officer", [{"material_id": "M-NOTE"}],
                external_ref="NOTE-FROZEN-1",
            )

    def test_dispute_at_requested_blocks_confirmation_and_handover(self):
        cid = "CASE-2026-0001"
        drive_to(self.engine, "requested")
        self.engine.raise_dispute(cid, "officer", reason="对方来函内容有争议")

        self.engine.register_material(
            cid, "custodian", material_id="M-IN", kind="diplomatic_note",
            title="确认", external_ref="NOTE-IN-FROZEN",
        )
        with self.assertRaises(OrderingError):
            self.engine.record_confirmation(
                cid, "custodian", [{"material_id": "M-IN"}],
                external_ref="NOTE-IN-FROZEN", in_reply_to="NOTE-OUT-0001",
            )
        with self.assertRaises(OrderingError):
            self.engine.record_approval(
                cid, "reviewer", action="complete_handover", approved=True
            )

    def test_duplicate_dispute_rejected(self):
        cid = "CASE-2026-0001"
        drive_to(self.engine, "intake")
        self.engine.raise_dispute(cid, "officer", reason="第一次")
        with self.assertRaises(OrderingError):
            self.engine.raise_dispute(cid, "officer", reason="第二次")

    def test_resolve_restores_flow(self):
        cid = "CASE-2026-0001"
        drive_to(self.engine, "attribution")
        self.engine.raise_dispute(cid, "reviewer", reason="鉴定依据争议")
        with self.assertRaises(OrderingError):
            self.engine.record_approval(
                cid, "approver", action="issue_request", approved=True
            )
        self.engine.resolve_dispute(
            cid, "reviewer", resolution="补充鉴定后争议消除"
        )
        self.assertFalse(self.engine.get_state(cid).disputed)
        # 恢复后流程可继续
        self.engine.record_approval(
            cid, "approver", action="issue_request", approved=True
        )

    def test_resolve_without_dispute_rejected(self):
        cid = "CASE-2026-0001"
        drive_to(self.engine, "intake")
        with self.assertRaises(OrderingError):
            self.engine.resolve_dispute(cid, "reviewer", resolution="无争议")

    def test_dispute_history_is_retained(self):
        cid = "CASE-2026-0001"
        drive_to(self.engine, "intake")
        self.engine.raise_dispute(cid, "officer", reason="争议一")
        self.engine.resolve_dispute(cid, "officer", resolution="解决一")
        view = self.engine.case_view(cid)
        self.assertEqual(len(view["dispute_history"]), 1)
        self.assertEqual(view["dispute_history"][0]["resolution"], "解决一")

    def test_archived_case_cannot_be_disputed(self):
        cid = "CASE-2026-0001"
        drive_to(self.engine, "archived")
        with self.assertRaises(OrderingError):
            self.engine.raise_dispute(cid, "officer", reason="太迟了")

    def test_case_view_marks_restricted(self):
        cid = "CASE-2026-0001"
        drive_to(self.engine, "intake")
        self.engine.raise_dispute(cid, "officer", reason="x")
        view = self.engine.case_view(cid)
        self.assertTrue(view["restricted"])
        self.assertTrue(view["disputed"])


if __name__ == "__main__":
    unittest.main()
