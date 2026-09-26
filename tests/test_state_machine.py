import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import helpers  # noqa: E402
from src.relic_case.errors import ConflictError, InvalidTransitionError  # noqa: E402


class StateMachineTests(unittest.TestCase):
    """不可跳步的状态规则与责任视图。"""

    def setUp(self):
        self.service = helpers.make_service()
        self.case_id = helpers.make_case(self.service)["case_id"]

    def test_full_chain_closes(self):
        helpers.drive_to_archived(self.service, self.case_id)
        status = self.service.get_case(self.case_id)
        self.assertEqual(status["stage"], "archived")
        self.assertEqual(status["stage_label"], "结案归档")
        self.assertTrue(status["handover_chain_closed"])
        self.assertEqual(status["pending_conditions"], [])
        self.assertEqual(status["responsible"]["role"], None)
        chain = status["chain"]
        self.assertEqual(chain["request"]["external_id"], "NOTE-OUT-2026-0001")
        self.assertEqual(chain["confirmation"]["external_id"], "NOTE-IN-2026-0001")
        self.assertEqual(chain["receipt"]["external_id"], "RCPT-2026-0001")
        self.assertTrue(chain["closed"])

    def test_stage_progresses_step_by_step(self):
        service, cid = self.service, self.case_id
        self.assertEqual(service.get_case(cid)["stage"], "intake")
        helpers.step_seal(service, cid)
        self.assertEqual(service.get_case(cid)["stage"], "evidence_sealed")
        helpers.step_appraise(service, cid)
        self.assertEqual(service.get_case(cid)["stage"], "attribution")
        helpers.step_request(service, cid)
        self.assertEqual(service.get_case(cid)["stage"], "requested")
        helpers.step_confirm(service, cid)
        self.assertEqual(service.get_case(cid)["stage"], "confirmed")
        helpers.step_handover(service, cid)
        self.assertEqual(service.get_case(cid)["stage"], "handover")
        helpers.step_close(service, cid)
        self.assertEqual(service.get_case(cid)["stage"], "archived")

    def test_cannot_skip_to_confirmation_from_intake(self):
        with self.assertRaises(InvalidTransitionError):
            self.service.perform(
                "officer-1",
                self.case_id,
                "record_confirmation",
                {"external_id": "NOTE-IN-2026-0001"},
            )

    def test_cannot_draft_request_before_attribution(self):
        helpers.drive_to_evidence_sealed(self.service, self.case_id)
        with self.assertRaises(InvalidTransitionError):
            self.service.perform(
                "officer-1", self.case_id, "draft_request", {"summary": "请求函"}
            )

    def test_cannot_close_before_handover(self):
        helpers.drive_to_requested(self.service, self.case_id)
        with self.assertRaises(InvalidTransitionError):
            self.service.perform("approver-1", self.case_id, "close_case")

    def test_cannot_record_handover_before_confirmation(self):
        helpers.drive_to_requested(self.service, self.case_id)
        with self.assertRaises(InvalidTransitionError):
            self.service.perform(
                "custodian-1",
                self.case_id,
                "record_handover",
                {"external_id": "RCPT-2026-0001"},
            )

    def test_seal_requires_seizure_record(self):
        with self.assertRaises(InvalidTransitionError):
            self.service.perform("officer-1", self.case_id, "seal_evidence")

    def test_appraisal_must_be_reviewed_before_request(self):
        helpers.drive_to_evidence_sealed(self.service, self.case_id)
        self.service.perform(
            "expert-1", self.case_id, "submit_appraisal", {"conclusion": "西周"}
        )
        with self.assertRaises(InvalidTransitionError):
            self.service.perform(
                "officer-1", self.case_id, "draft_request", {"summary": "请求函"}
            )

    def test_double_approve_attribution_rejected(self):
        helpers.drive_to_evidence_sealed(self.service, self.case_id)
        self.service.perform(
            "expert-1", self.case_id, "submit_appraisal", {"conclusion": "西周"}
        )
        self.service.perform("reviewer-1", self.case_id, "approve_attribution")
        with self.assertRaises(InvalidTransitionError):
            self.service.perform("reviewer-1", self.case_id, "approve_attribution")

    def test_rejected_appraisal_can_be_resubmitted(self):
        service, cid = self.service, self.case_id
        helpers.drive_to_evidence_sealed(service, cid)
        service.perform("expert-1", cid, "submit_appraisal", {"conclusion": "待考"})
        service.perform(
            "reviewer-1", cid, "reject_attribution", {"reason": "依据不足"}
        )
        status = service.get_case(cid)
        self.assertEqual(status["stage"], "evidence_sealed")
        opinions = [m for m in status["materials"] if m["kind"] == "expert_opinion"]
        self.assertEqual(opinions[0]["status"], "rejected")
        service.perform("expert-1", cid, "submit_appraisal", {"conclusion": "西周"})
        opinions = [
            m
            for m in service.get_case(cid)["materials"]
            if m["kind"] == "expert_opinion"
        ]
        self.assertEqual({m["version"] for m in opinions}, {1, 2})
        service.perform("reviewer-1", cid, "approve_attribution")
        self.assertEqual(service.get_case(cid)["stage"], "attribution")

    def test_second_appraisal_while_pending_rejected(self):
        helpers.drive_to_evidence_sealed(self.service, self.case_id)
        self.service.perform(
            "expert-1", self.case_id, "submit_appraisal", {"conclusion": "西周"}
        )
        with self.assertRaises(ConflictError):
            self.service.perform(
                "expert-1", self.case_id, "submit_appraisal", {"conclusion": "东周"}
            )

    def test_status_reports_responsible_and_missing_conditions(self):
        status = self.service.get_case(self.case_id)
        self.assertEqual(status["responsible"]["role"], "case_officer")
        codes = {c["code"] for c in status["pending_conditions"]}
        self.assertIn("need_seizure_record", codes)
        self.assertIn("seal_evidence", codes)

        helpers.drive_to_requested(self.service, self.case_id)
        status = self.service.get_case(self.case_id)
        self.assertEqual(status["responsible"]["role"], "external_counterparty")
        codes = {c["code"] for c in status["pending_conditions"]}
        self.assertEqual(codes, {"await_confirmation"})

    def test_archived_case_rejects_further_actions(self):
        helpers.drive_to_archived(self.service, self.case_id)
        with self.assertRaises(InvalidTransitionError):
            self.service.perform(
                "officer-1", self.case_id, "raise_dispute", {"reason": "补充争议"}
            )
        with self.assertRaises(InvalidTransitionError):
            self.service.submit_material(
                "officer-1", self.case_id, "ownership_claim", "事后补充"
            )

    def test_decision_events_keep_material_version_snapshot(self):
        helpers.drive_to_attribution(self.service, self.case_id)
        events = self.service.list_events(self.case_id)
        sealed = next(e for e in events if e["type"] == "evidence_sealed")
        refs = {r["material_id"]: r["version"] for r in sealed["payload"]["material_refs"]}
        seizure = next(
            m
            for m in self.service.get_case(self.case_id)["materials"]
            if m["kind"] == "seizure_record"
        )
        self.assertEqual(refs[seizure["material_id"]], seizure["version"])
        approved = next(e for e in events if e["type"] == "attribution_approved")
        self.assertTrue(approved["payload"]["material_refs"])
        self.assertEqual(approved["actor_id"], "reviewer-1")
        self.assertTrue(approved["occurred_at"])


if __name__ == "__main__":
    unittest.main()
