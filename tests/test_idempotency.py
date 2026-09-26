import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import helpers  # noqa: E402
from src.relic_case.errors import ConflictError, ValidationError  # noqa: E402


class IdempotencyTests(unittest.TestCase):
    """同一外部函件/回执重复送达不得造成重复推进。"""

    def setUp(self):
        self.service = helpers.make_service()
        self.case_id = helpers.make_case(self.service)["case_id"]

    def _events_of(self, event_type):
        return [
            e
            for e in self.service.list_events(self.case_id)
            if e["type"] == event_type and e["voided_by"] is None
        ]

    def test_duplicate_confirmation_letter_not_double_applied(self):
        helpers.drive_to_requested(self.service, self.case_id)
        first = self.service.perform(
            "officer-1",
            self.case_id,
            "record_confirmation",
            {"external_id": "NOTE-IN-2026-0001"},
        )
        self.assertFalse(first["deduplicated"])
        second = self.service.perform(
            "officer-1",
            self.case_id,
            "record_confirmation",
            {"external_id": "NOTE-IN-2026-0001"},
        )
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["event_id"], first["event_id"])
        self.assertEqual(len(self._events_of("confirmation_recorded")), 1)
        self.assertEqual(self.service.get_case(self.case_id)["stage"], "confirmed")

    def test_duplicate_handover_receipt_not_double_applied(self):
        helpers.drive_to_confirmed(self.service, self.case_id)
        first = self.service.perform(
            "custodian-1",
            self.case_id,
            "record_handover",
            {"external_id": "RCPT-2026-0001"},
        )
        second = self.service.perform(
            "custodian-1",
            self.case_id,
            "record_handover",
            {"external_id": "RCPT-2026-0001"},
        )
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["event_id"], first["event_id"])
        self.assertEqual(len(self._events_of("handover_recorded")), 1)
        receipts = [
            m
            for m in self.service.get_case(self.case_id)["materials"]
            if m["kind"] == "handover_receipt"
        ]
        self.assertEqual(len(receipts), 1)

    def test_duplicate_material_submission_returns_existing(self):
        first = self.service.submit_material(
            "officer-1",
            self.case_id,
            "seizure_record",
            "海关查扣记录",
            external_id="SEIZE-2026-0001",
        )
        second = self.service.submit_material(
            "officer-1",
            self.case_id,
            "seizure_record",
            "海关查扣记录（重发）",
            external_id="SEIZE-2026-0001",
        )
        self.assertTrue(second["deduplicated"])
        self.assertEqual(
            second["material"]["material_id"], first["material"]["material_id"]
        )
        self.assertEqual(len(self.service.get_case(self.case_id)["materials"]), 1)

    def test_duplicate_draft_request_not_double_applied(self):
        helpers.drive_to_attribution(self.service, self.case_id)
        first = self.service.perform(
            "officer-1",
            self.case_id,
            "draft_request",
            {"summary": "请求函", "external_id": "NOTE-OUT-2026-0001"},
        )
        second = self.service.perform(
            "officer-1",
            self.case_id,
            "draft_request",
            {"summary": "请求函", "external_id": "NOTE-OUT-2026-0001"},
        )
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["event_id"], first["event_id"])
        self.assertEqual(len(self._events_of("request_drafted")), 1)

    def test_external_id_conflict_across_cases(self):
        helpers.drive_to_requested(self.service, self.case_id)
        self.service.perform(
            "officer-1",
            self.case_id,
            "record_confirmation",
            {"external_id": "NOTE-IN-2026-0001"},
        )
        other_id = helpers.make_case(self.service)["case_id"]
        helpers.drive_to_requested(self.service, other_id, suffix="-B")
        with self.assertRaises(ConflictError):
            self.service.perform(
                "officer-1",
                other_id,
                "record_confirmation",
                {"external_id": "NOTE-IN-2026-0001"},
            )

    def test_external_id_reused_for_other_purpose_in_same_case(self):
        self.service.submit_material(
            "officer-1",
            self.case_id,
            "seizure_record",
            "查扣记录",
            external_id="SEIZE-2026-0001",
        )
        self.service.perform("officer-1", self.case_id, "seal_evidence")
        self.service.perform(
            "expert-1", self.case_id, "submit_appraisal", {"conclusion": "西周"}
        )
        self.service.perform("reviewer-1", self.case_id, "approve_attribution")
        with self.assertRaises(ConflictError):
            self.service.perform(
                "officer-1",
                self.case_id,
                "draft_request",
                {"summary": "请求函", "external_id": "SEIZE-2026-0001"},
            )

    def test_invalid_external_id_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.submit_material(
                "officer-1",
                self.case_id,
                "seizure_record",
                "查扣记录",
                external_id="短",
            )

    def test_confirmation_requires_external_id(self):
        helpers.drive_to_requested(self.service, self.case_id)
        with self.assertRaises(ValidationError):
            self.service.perform(
                "officer-1", self.case_id, "record_confirmation", {}
            )


if __name__ == "__main__":
    unittest.main()
