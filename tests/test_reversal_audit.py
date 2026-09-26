"""撤销错误录入：冲正而非删除，历史完整保留，阶段正确回退。"""

import unittest

from src.relic_case.engine import DomainError, OrderingError, build_ledger
from support import build_engine, drive_to, open_case


class ReversalTests(unittest.TestCase):
    def setUp(self):
        self.engine = build_engine()
        self.cid = open_case(self.engine)

    def _seal_with_bad_record(self):
        self.engine.register_material(
            self.cid, "officer", material_id="M-BAD", kind="seizure_record",
            title="录入有误的查获记录",
        )
        self.engine.seal_evidence(
            self.cid, "officer", [{"material_id": "M-BAD"}], seal_id="SEAL-1"
        )

    def test_reverse_wrong_material_at_intake(self):
        self.engine.register_material(
            self.cid, "officer", material_id="M-TYPO", kind="seizure_record",
            title="错把函件登成查获记录",
        )
        seq = self.engine.get_state(self.cid).materials["M-TYPO"][0].event_seq
        result = self.engine.reverse_entry(
            self.cid, "reviewer", target_seq=seq, reason="类别登记错误"
        )
        self.assertIn("seq", result)

        state = self.engine.get_state(self.cid)
        self.assertTrue(state.material("M-TYPO").reversed)
        # 该材料不能再作为决定依据
        with self.assertRaises(DomainError) as ctx:
            self.engine.seal_evidence(
                self.cid, "officer", [{"material_id": "M-TYPO"}], seal_id="S"
            )
        self.assertEqual(ctx.exception.code, "reversed_material")

    def test_reverse_attribution_rolls_stage_back(self):
        self._seal_with_bad_record()
        self.engine.register_material(
            self.cid, "expert", material_id="M-OP", kind="expert_opinion",
            title="初版鉴定",
        )
        attr_seq = self.engine.get_state(self.cid).links  # noqa
        self.engine.record_attribution(
            self.cid, "expert", [{"material_id": "M-OP"}], conclusion="初判"
        )
        self.assertEqual(self.engine.get_state(self.cid).stage, "attribution")
        target = [
            e for e in self.engine.get_state(self.cid).events
            if e["event_type"] == "attribution_recorded"
        ][0]["seq"]
        self.engine.reverse_entry(self.cid, "officer", target, "鉴定结论依据不足")
        # 阶段回退、鉴定人不再被视为本案专家
        state = self.engine.get_state(self.cid)
        self.assertEqual(state.stage, "evidence_sealed")
        self.assertNotIn("expert", state.experts)
        self.assertNotIn("attribution", state.links)

    def test_original_event_still_visible_in_ledger(self):
        self._seal_with_bad_record()
        target = [
            e for e in self.engine.get_state(self.cid).events
            if e["event_type"] == "evidence_sealed"
        ][0]["seq"]
        self.engine.reverse_entry(self.cid, "reviewer", target, "封存程序瑕疵")
        ledger = build_ledger(self.engine.get_state(self.cid), self.engine._users)
        types = [(e["event_type"], e["reversal_of_seq"]) for e in ledger["event_log"]]
        self.assertIn(("evidence_sealed", None), types)
        self.assertIn(("entry_reversed", target), types)
        # 被冲正的决定在监管台账中明确标注，且不被删除
        sealed = [d for d in ledger["decisions"] if d["decision"] == "evidence_sealed"]
        self.assertTrue(sealed[0]["reversed"])

    def test_cannot_reverse_passed_stage_node(self):
        drive_to(self.engine, "attribution")
        target = [
            e for e in self.engine.get_state(self.cid).events
            if e["event_type"] == "evidence_sealed"
        ][0]["seq"]
        with self.assertRaises(OrderingError):
            self.engine.reverse_entry(self.cid, "officer", target, "想倒改封存")

    def test_cannot_reverse_non_reversible_type(self):
        target = 1  # case_opened
        with self.assertRaises(DomainError) as ctx:
            self.engine.reverse_entry(self.cid, "officer", target, "撤案")
        self.assertEqual(ctx.exception.code, "not_reversible")

    def test_cannot_reverse_twice(self):
        self.engine.register_material(
            self.cid, "officer", material_id="M", kind="seizure_record", title="t"
        )
        seq = self.engine.get_state(self.cid).materials["M"][0].event_seq
        self.engine.reverse_entry(self.cid, "officer", seq, "第一次")
        with self.assertRaises(DomainError) as ctx:
            self.engine.reverse_entry(self.cid, "officer", seq, "第二次")
        self.assertEqual(ctx.exception.code, "already_reversed")

    def test_cannot_reverse_material_referenced_by_later_decision(self):
        self._seal_with_bad_record()
        target = self.engine.get_state(self.cid).materials["M-BAD"][0].event_seq
        with self.assertRaises(OrderingError):
            self.engine.reverse_entry(self.cid, "reviewer", target, "已被封存引用")

    def test_reverse_requires_permission(self):
        self.engine.register_material(
            self.cid, "custodian", material_id="M", kind="seizure_record", title="t"
        )
        seq = self.engine.get_state(self.cid).materials["M"][0].event_seq
        from src.relic_case.engine import AuthzError

        with self.assertRaises(AuthzError):
            self.engine.reverse_entry(self.cid, "custodian", seq, "无权")

    def test_reversal_blocked_during_dispute(self):
        self.engine.register_material(
            self.cid, "officer", material_id="M", kind="seizure_record", title="t"
        )
        seq = self.engine.get_state(self.cid).materials["M"][0].event_seq
        self.engine.raise_dispute(self.cid, "officer", "争议中")
        with self.assertRaises(OrderingError):
            self.engine.reverse_entry(self.cid, "officer", seq, "争议时冲正")

    def test_every_decision_records_basis_actor_and_time(self):
        drive_to(self.engine, "requested")
        ledger = build_ledger(self.engine.get_state(self.cid), self.engine._users)
        for d in ledger["decisions"]:
            self.assertTrue(d["actor_id"])
            self.assertTrue(d["actor_role"])
            self.assertIn("decided_at", d)
            if d["decision"] in {
                "evidence_sealed",
                "attribution_recorded",
                "request_issued",
                "counterpart_confirmed",
            }:
                self.assertTrue(
                    d["material_basis"], f"{d['decision']} 必须固化材料版本"
                )
        # 材料依据含不可变版本与哈希
        seal = next(d for d in ledger["decisions"] if d["decision"] == "evidence_sealed")
        basis = seal["material_basis"][0]
        self.assertEqual(basis["version"], 1)
        self.assertIn("event_seq", basis)


if __name__ == "__main__":
    unittest.main()
