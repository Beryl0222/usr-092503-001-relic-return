import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import helpers  # noqa: E402
from src.relic_case.errors import (  # noqa: E402
    ConflictError,
    InvalidTransitionError,
    OnHoldError,
)


class DisputeTests(unittest.TestCase):
    """争议中的物件只能停留在受限状态。"""

    def setUp(self):
        self.service = helpers.make_service()
        self.case_id = helpers.make_case(self.service)["case_id"]

    def test_hold_blocks_all_progression_until_resolved(self):
        service, cid = self.service, self.case_id
        helpers.drive_to_evidence_sealed(service, cid)
        service.perform("officer-1", cid, "raise_dispute", {"reason": "权属存在争议"})
        status = service.get_case(cid)
        self.assertTrue(status["held"])
        self.assertEqual(status["hold_reason"], "权属存在争议")
        self.assertEqual(status["responsible"]["role"], "reviewer")
        codes = {c["code"] for c in status["pending_conditions"]}
        self.assertEqual(codes, {"dispute_hold"})

        with self.assertRaises(OnHoldError):
            service.perform(
                "expert-1", cid, "submit_appraisal", {"conclusion": "西周"}
            )
        with self.assertRaises(OnHoldError):
            service.submit_material("officer-1", cid, "ownership_claim", "权属主张")
        with self.assertRaises(OnHoldError):
            service.perform("officer-1", cid, "seal_evidence")

        service.perform("reviewer-1", cid, "resolve_dispute", {"note": "争议已排除"})
        self.assertFalse(service.get_case(cid)["held"])
        service.perform("expert-1", cid, "submit_appraisal", {"conclusion": "西周"})
        self.assertEqual(
            service.get_case(cid)["responsible"]["role"], "reviewer"
        )

    def test_hold_blocks_void(self):
        service, cid = self.service, self.case_id
        service.submit_material("officer-1", cid, "seizure_record", "查扣记录")
        service.perform("officer-1", cid, "raise_dispute", {"reason": "争议"})
        events = service.list_events(cid)
        material_event = next(e for e in events if e["type"] == "material_submitted")
        with self.assertRaises(OnHoldError):
            service.void_event("officer-1", cid, material_event["event_id"], "撤销")

    def test_resolve_without_hold_fails(self):
        with self.assertRaises(InvalidTransitionError):
            self.service.perform("reviewer-1", self.case_id, "resolve_dispute")

    def test_double_raise_fails(self):
        self.service.perform(
            "officer-1", self.case_id, "raise_dispute", {"reason": "争议"}
        )
        with self.assertRaises(ConflictError):
            self.service.perform(
                "reviewer-1", self.case_id, "raise_dispute", {"reason": "再次争议"}
            )

    def test_raise_requires_reason(self):
        from src.relic_case.errors import ValidationError

        with self.assertRaises(ValidationError):
            self.service.perform("officer-1", self.case_id, "raise_dispute", {})

    def test_dispute_keeps_stage_visible(self):
        helpers.drive_to_requested(self.service, self.case_id)
        self.service.perform(
            "officer-1", self.case_id, "raise_dispute", {"reason": "回执真伪存疑"}
        )
        status = self.service.get_case(self.case_id)
        self.assertEqual(status["stage"], "requested")
        self.assertTrue(status["held"])
        self.service.perform("approver-1", self.case_id, "resolve_dispute")
        self.assertFalse(self.service.get_case(self.case_id)["held"])
        # 解除后可继续推进
        self.service.perform(
            "officer-1",
            self.case_id,
            "record_confirmation",
            {"external_id": "NOTE-IN-2026-0001"},
        )
        self.assertEqual(self.service.get_case(self.case_id)["stage"], "confirmed")


if __name__ == "__main__":
    unittest.main()
