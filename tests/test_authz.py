"""越权访问与职责分离：原鉴定人不得复核/批准关键动作。"""

import unittest

from src.relic_case.engine import AuthzError, DomainError
from support import build_engine, drive_to, open_case


class AuthzTests(unittest.TestCase):
    def setUp(self):
        self.engine = build_engine()
        self.cid = open_case(self.engine)

    def test_unknown_actor_rejected(self):
        with self.assertRaises(AuthzError):
            self.engine.register_material(
                self.cid, "ghost", material_id="M", kind="seizure_record",
                title="x",
            )

    def test_wrong_role_cannot_seal(self):
        with self.assertRaises(AuthzError):
            self.engine.seal_evidence(self.cid, "expert", [], seal_id="S")

    def test_wrong_role_cannot_record_attribution(self):
        drive_to(self.engine, "evidence_sealed")
        with self.assertRaises(AuthzError):
            self.engine.record_attribution(
                self.cid, "officer", [], conclusion="x"
            )

    def test_expert_cannot_approve_request(self):
        drive_to(self.engine, "attribution")
        with self.assertRaises(AuthzError):
            self.engine.record_approval(
                self.cid, "expert", action="issue_request", approved=True
            )

    def test_original_expert_cannot_approve_even_with_approver_role(self):
        # expert_boss 同时具备 expert 与 approver 职责，且亲自做了原鉴定
        drive_to(self.engine, "evidence_sealed")
        self.engine.register_material(
            self.cid, "expert_boss", material_id="M-OPINION",
            kind="expert_opinion", title="鉴定书",
        )
        self.engine.record_attribution(
            self.cid, "expert_boss", [{"material_id": "M-OPINION"}],
            conclusion="春秋",
        )
        with self.assertRaises(AuthzError) as ctx:
            self.engine.record_approval(
                self.cid, "expert_boss", action="issue_request", approved=True
            )
        self.assertIn("原鉴定", str(ctx.exception))

    def test_independent_expert_can_approve(self):
        # 另一未参与鉴定的专家（兼批准职责）可以批准
        drive_to(self.engine, "attribution")
        result = self.engine.record_approval(
            self.cid, "expert_boss", action="issue_request", approved=True
        )
        self.assertIn("seq", result)

    def test_custodian_cannot_issue_request(self):
        drive_to(self.engine, "attribution")
        self.engine.record_approval(
            self.cid, "approver", action="issue_request", approved=True
        )
        with self.assertRaises(AuthzError):
            self.engine.issue_request(
                self.cid, "custodian", [], external_ref="NOTE-OUT-0001"
            )

    def test_only_custodian_completes_handover(self):
        drive_to(self.engine, "requested")
        self.engine.register_material(
            self.cid, "custodian", material_id="M-NOTE-IN",
            kind="diplomatic_note", title="确认", external_ref="NOTE-IN-0001",
        )
        self.engine.record_confirmation(
            self.cid, "custodian", [{"material_id": "M-NOTE-IN"}],
            external_ref="NOTE-IN-0001", in_reply_to="NOTE-OUT-0001",
        )
        self.engine.record_approval(
            self.cid, "reviewer", action="complete_handover", approved=True
        )
        self.engine.register_material(
            self.cid, "custodian", material_id="M-R",
            kind="handover_receipt", title="回执", external_ref="AUTHZ-RCPT-01",
        )
        with self.assertRaises(AuthzError):
            self.engine.complete_handover(
                self.cid, "officer",
                [{"material_id": "M-R"}], external_ref="AUTHZ-RCPT-01",
            )

    def test_officer_cannot_approve_own_chain(self):
        drive_to(self.engine, "handover")
        with self.assertRaises(AuthzError):
            self.engine.record_approval(
                self.cid, "officer", action="archive", approved=True
            )

    def test_unknown_action_rejected(self):
        drive_to(self.engine, "attribution")
        with self.assertRaises(DomainError):
            self.engine.record_approval(
                self.cid, "approver", action="destroy", approved=True
            )

    def test_unknown_case_is_404(self):
        from src.relic_case.engine import NotFoundError

        with self.assertRaises(NotFoundError):
            self.engine.get_state("CASE-NOPE-0000")


if __name__ == "__main__":
    unittest.main()
