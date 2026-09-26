"""乱序与跳步：任何缺失前置条件的推进都必须被拒绝。"""

import unittest

from src.relic_case.engine import DomainError, OrderingError
from support import build_engine, drive_to, open_case


class OrderingTests(unittest.TestCase):
    def setUp(self):
        self.engine = build_engine()
        self.cid = open_case(self.engine)

    def test_cannot_seal_before_material(self):
        with self.assertRaises(DomainError) as ctx:
            self.engine.seal_evidence(
                self.cid, "officer", [], seal_id="SEAL-1"
            )
        self.assertEqual(ctx.exception.code, "missing_seizure_record")

    def test_cannot_seal_with_non_seizure_material(self):
        self.engine.register_material(
            self.cid, "officer", material_id="M", kind="ownership_claim",
            title="权属主张",
        )
        with self.assertRaises(DomainError) as ctx:
            self.engine.seal_evidence(
                self.cid, "officer", [{"material_id": "M"}], seal_id="SEAL-1"
            )
        self.assertEqual(ctx.exception.code, "missing_seizure_record")

    def test_stages_cannot_be_skipped(self):
        # 尚未封存证据，直接鉴定
        with self.assertRaises(OrderingError):
            self.engine.record_attribution(
                self.cid, "expert", [], conclusion="x"
            )
        # 直接发对外请求
        with self.assertRaises(OrderingError):
            self.engine.issue_request(
                self.cid, "officer", [], external_ref="NOTE-OUT-0001"
            )
        # 直接交接
        with self.assertRaises(OrderingError):
            self.engine.complete_handover(
                self.cid, "custodian", [], external_ref="RECEIPT-0001"
            )
        # 直接归档
        with self.assertRaises(OrderingError):
            self.engine.archive_case(self.cid, "officer", archive_location="库")

    def test_request_requires_prior_approval(self):
        drive_to(self.engine, "attribution")
        self.engine.register_material(
            self.cid, "officer", material_id="M-NOTE-OUT",
            kind="diplomatic_note", title="函", external_ref="NOTE-OUT-0001",
        )
        with self.assertRaises(OrderingError):
            self.engine.issue_request(
                self.cid, "officer",
                [{"material_id": "M-NOTE-OUT"}],
                external_ref="NOTE-OUT-0001",
            )

    def test_handover_requires_confirmation_and_approval(self):
        drive_to(self.engine, "requested")
        self.engine.register_material(
            self.cid, "custodian", material_id="M-RECEIPT",
            kind="handover_receipt", title="回执", external_ref="RECEIPT-0001",
        )
        # 缺确认函
        with self.assertRaises(OrderingError):
            self.engine.complete_handover(
                self.cid, "custodian",
                [{"material_id": "M-RECEIPT"}], external_ref="RECEIPT-0001",
            )
        self.engine.register_material(
            self.cid, "custodian", material_id="M-NOTE-IN",
            kind="diplomatic_note", title="确认", external_ref="NOTE-IN-0001",
        )
        self.engine.record_confirmation(
            self.cid, "custodian", [{"material_id": "M-NOTE-IN"}],
            external_ref="NOTE-IN-0001", in_reply_to="NOTE-OUT-0001",
        )
        # 有确认但缺批准
        with self.assertRaises(OrderingError):
            self.engine.complete_handover(
                self.cid, "custodian",
                [{"material_id": "M-RECEIPT"}], external_ref="RECEIPT-0001",
            )

    def test_confirmation_must_reply_to_actual_request(self):
        drive_to(self.engine, "requested")
        self.engine.register_material(
            self.cid, "custodian", material_id="M-NOTE-IN",
            kind="diplomatic_note", title="确认", external_ref="NOTE-IN-0001",
        )
        with self.assertRaises(DomainError) as ctx:
            self.engine.record_confirmation(
                self.cid, "custodian", [{"material_id": "M-NOTE-IN"}],
                external_ref="NOTE-IN-0001", in_reply_to="NOTE-OTHER-9999",
            )
        self.assertEqual(ctx.exception.code, "ref_mismatch")

    def test_approval_only_in_matching_stage(self):
        drive_to(self.engine, "evidence_sealed")
        with self.assertRaises(OrderingError):
            self.engine.record_approval(
                self.cid, "approver", action="issue_request", approved=True
            )

    def test_rejected_approval_does_not_advance(self):
        drive_to(self.engine, "attribution")
        self.engine.record_approval(
            self.cid, "approver", action="issue_request", approved=False,
            note="材料不足，退回",
        )
        self.engine.register_material(
            self.cid, "officer", material_id="M-NOTE-OUT",
            kind="diplomatic_note", title="函", external_ref="NOTE-OUT-0001",
        )
        with self.assertRaises(OrderingError):
            self.engine.issue_request(
                self.cid, "officer",
                [{"material_id": "M-NOTE-OUT"}],
                external_ref="NOTE-OUT-0001",
            )

    def test_cannot_register_material_that_references_unknown(self):
        with self.assertRaises(DomainError) as ctx:
            self.engine.seal_evidence(
                self.cid, "officer", [{"material_id": "GHOST"}], seal_id="S"
            )
        self.assertEqual(ctx.exception.code, "unknown_material")


if __name__ == "__main__":
    unittest.main()
