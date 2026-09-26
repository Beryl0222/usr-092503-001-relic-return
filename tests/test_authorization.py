import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import helpers  # noqa: E402
from src.relic_case.errors import ForbiddenError, UnknownActorError  # noqa: E402


class AuthorizationTests(unittest.TestCase):
    """越权与职责分离：复核/批准人不得参与原鉴定。"""

    def setUp(self):
        self.service = helpers.make_service()
        self.case_id = helpers.make_case(self.service)["case_id"]

    def test_unknown_actor_rejected(self):
        with self.assertRaises(UnknownActorError):
            self.service.perform("nobody", self.case_id, "seal_evidence")

    def test_wrong_role_forbidden(self):
        helpers.drive_to_evidence_sealed(self.service, self.case_id)
        with self.assertRaises(ForbiddenError):
            self.service.perform("expert-1", self.case_id, "seal_evidence")
        with self.assertRaises(ForbiddenError):
            self.service.perform("officer-1", self.case_id, "approve_attribution")
        with self.assertRaises(ForbiddenError):
            self.service.perform("custodian-1", self.case_id, "close_case")

    def test_create_case_requires_case_officer(self):
        with self.assertRaises(ForbiddenError):
            self.service.create_case("expert-1", "越权案件", {"name": "陶俑"})

    def test_appraiser_cannot_review_own_opinion(self):
        helpers.drive_to_evidence_sealed(self.service, self.case_id)
        # expert-2 同时具备 reviewer 职责，但参与了原鉴定
        self.service.perform(
            "expert-2", self.case_id, "submit_appraisal", {"conclusion": "西周"}
        )
        with self.assertRaises(ForbiddenError):
            self.service.perform("expert-2", self.case_id, "approve_attribution")
        # 未参与鉴定的复核人可以批准
        self.service.perform("reviewer-1", self.case_id, "approve_attribution")
        self.assertEqual(self.service.get_case(self.case_id)["stage"], "attribution")

    def test_appraisal_co_author_cannot_approve_request(self):
        helpers.drive_to_evidence_sealed(self.service, self.case_id)
        self.service.perform(
            "expert-1",
            self.case_id,
            "submit_appraisal",
            {"conclusion": "西周", "co_authors": ["approver-2"]},
        )
        self.service.perform("reviewer-1", self.case_id, "approve_attribution")
        self.service.perform(
            "officer-1", self.case_id, "draft_request", {"summary": "请求函"}
        )
        with self.assertRaises(ForbiddenError):
            self.service.perform("approver-2", self.case_id, "approve_request")
        self.service.perform("approver-1", self.case_id, "approve_request")
        self.assertEqual(self.service.get_case(self.case_id)["stage"], "requested")

    def test_appraisal_participant_cannot_close_case(self):
        helpers.drive_to_evidence_sealed(self.service, self.case_id)
        self.service.perform(
            "expert-1",
            self.case_id,
            "submit_appraisal",
            {"conclusion": "西周", "co_authors": ["approver-2"]},
        )
        self.service.perform("reviewer-1", self.case_id, "approve_attribution")
        self.service.perform(
            "officer-1", self.case_id, "draft_request", {"summary": "请求函"}
        )
        self.service.perform("approver-1", self.case_id, "approve_request")
        self.service.perform(
            "officer-1",
            self.case_id,
            "record_confirmation",
            {"external_id": "NOTE-IN-2026-0001"},
        )
        self.service.perform(
            "custodian-1",
            self.case_id,
            "record_handover",
            {"external_id": "RCPT-2026-0001"},
        )
        with self.assertRaises(ForbiddenError):
            self.service.perform("approver-2", self.case_id, "close_case")
        self.service.perform("approver-1", self.case_id, "close_case")
        self.assertEqual(self.service.get_case(self.case_id)["stage"], "archived")

    def test_resolve_dispute_requires_reviewer_or_approver(self):
        helpers.drive_to_evidence_sealed(self.service, self.case_id)
        self.service.perform(
            "officer-1", self.case_id, "raise_dispute", {"reason": "权属争议"}
        )
        with self.assertRaises(ForbiddenError):
            self.service.perform("custodian-1", self.case_id, "resolve_dispute")
        self.service.perform("reviewer-1", self.case_id, "resolve_dispute")
        self.assertFalse(self.service.get_case(self.case_id)["held"])

    def test_void_requires_case_officer(self):
        helpers.drive_to_evidence_sealed(self.service, self.case_id)
        events = self.service.list_events(self.case_id)
        material_event = next(e for e in events if e["type"] == "material_submitted")
        with self.assertRaises(ForbiddenError):
            self.service.void_event(
                "custodian-1", self.case_id, material_event["event_id"], "试图撤销"
            )


if __name__ == "__main__":
    unittest.main()
