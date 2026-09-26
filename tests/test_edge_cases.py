"""补充边界：迟到重复函件、版本固化、错误输入、并发建案、台账闭合。"""

import threading
import unittest

from src.relic_case.engine import (
    DomainError,
    DuplicateEventError,
    build_ledger,
)
from support import build_engine, drive_to


class EdgeCaseTests(unittest.TestCase):
    def setUp(self):
        self.engine = build_engine()
        self.cid = "CASE-2026-0001"

    def test_late_duplicate_confirmation_after_handover_still_detected(self):
        drive_to(self.engine, "handover")
        # 交接完成后，旧确认函因网络重发再次送达
        with self.assertRaises(DuplicateEventError) as ctx:
            self.engine.record_confirmation(
                self.cid, "custodian",
                [{"material_id": "M-NOTE-IN"}],
                external_ref="NOTE-IN-0001", in_reply_to="NOTE-OUT-0001",
            )
        self.assertEqual(ctx.exception.code, "duplicate_event")

    def test_decision_basis_pinned_to_version(self):
        drive_to(self.engine, "evidence_sealed")
        self.engine.register_material(
            self.cid, "expert", material_id="M-OP", kind="expert_opinion",
            title="意见书 v1", sha256="c" * 64,
        )
        self.engine.record_attribution(
            self.cid, "expert", [{"material_id": "M-OP"}], conclusion="初判"
        )
        # 鉴定之后再登记 v2（同一材料的新版本）
        self.engine.register_material(
            self.cid, "expert", material_id="M-OP", kind="expert_opinion",
            title="意见书 v2", sha256="d" * 64,
        )
        ledger = build_ledger(self.engine.get_state(self.cid), self.engine._users)
        decision = next(
            d for d in ledger["decisions"]
            if d["decision"] == "attribution_recorded"
        )
        # 决定依据永远指向 v1 及其固化时的事件序号，不受 v2 影响
        basis = decision["material_basis"][0]
        self.assertEqual(basis["version"], 1)
        self.assertEqual(basis["material_id"], "M-OP")
        view = self.engine.case_view(self.cid)
        mat = next(m for m in view["materials"] if m["material_id"] == "M-OP")
        self.assertEqual(mat["current_version"], 2)
        self.assertEqual(len(mat["versions"]), 2)

    def test_invalid_enum_returns_domain_error(self):
        drive_to(self.engine, "intake")
        with self.assertRaises(ValueError):
            self.engine.register_material(
                self.cid, "officer", material_id="M", kind="not_a_kind",
                title="t",
            )

    def test_short_external_ref_rejected(self):
        drive_to(self.engine, "intake")
        with self.assertRaises(ValueError):
            self.engine.register_material(
                self.cid, "officer", material_id="M", kind="diplomatic_note",
                title="t", external_ref="SHORT",
            )

    def test_open_case_requires_item_name(self):
        with self.assertRaises(DomainError) as ctx:
            self.engine.open_case(
                "CASE-2026-0200", "officer", {"category": "x"}
            )
        self.assertEqual(ctx.exception.code, "item_name_required")

    def test_concurrent_open_same_case_single_winner(self):
        outcomes = []
        barrier = threading.Barrier(2)

        def open_it():
            try:
                barrier.wait()
                outcomes.append(
                    self.engine.open_case(
                        "CASE-CONC-0001", "officer", {"name": "并发件"}
                    )
                )
            except Exception as exc:  # noqa: BLE001
                outcomes.append(exc)

        threads = [threading.Thread(target=open_it) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [o for o in outcomes if not isinstance(o, Exception)]
        self.assertEqual(len(successes), 1)
        self.assertTrue(
            any(isinstance(o, DuplicateEventError) for o in outcomes)
        )
        self.assertEqual(len(self.engine.list_cases()), 1)

    def test_ledger_shows_closed_chain_and_responsibility_none(self):
        drive_to(self.engine, "archived")
        view = self.engine.case_view(self.cid)
        self.assertIsNone(view["responsible"]["role"])
        self.assertTrue(view["chain"]["complete"])
        self.assertTrue(view["chain"]["refs_match"])
        labels = [l["label"] for l in view["chain"]["links"] if l["present"]]
        self.assertEqual(
            labels, ["证据封存", "专家鉴定", "对外请求", "对方确认", "交接回执", "结案归档"]
        )

    def test_reverse_material_then_reregister_new_version_allowed(self):
        drive_to(self.engine, "intake")
        self.engine.register_material(
            self.cid, "officer", material_id="M", kind="ownership_claim",
            title="误登为权属主张",
        )
        seq = self.engine.get_state(self.cid).materials["M"][0].event_seq
        self.engine.reverse_entry(self.cid, "officer", seq, "类别录错")
        # 冲正后以正确类别重新登记，同一材料编号产生新版本（v2）
        self.engine.register_material(
            self.cid, "officer", material_id="M", kind="seizure_record",
            title="查获记录",
        )
        versions = self.engine.get_state(self.cid).materials["M"]
        self.assertEqual(len(versions), 2)
        self.assertTrue(versions[0].reversed)
        self.assertFalse(versions[1].reversed)
        self.assertEqual(versions[1].version, 2)
        self.assertEqual(versions[1].kind, "seizure_record")

    def test_redone_attribution_still_requires_independent_approver(self):
        drive_to(self.engine, "attribution")
        target = [
            e for e in self.engine.get_state(self.cid).events
            if e["event_type"] == "attribution_recorded"
        ][0]["seq"]
        self.engine.reverse_entry(self.cid, "officer", target, "依据不足重鉴")
        # 原专家重做鉴定后，自己仍不能批准；由独立批准人批准
        self.engine.register_material(
            self.cid, "expert", material_id="M-OP2", kind="expert_opinion",
            title="重新鉴定书",
        )
        self.engine.record_attribution(
            self.cid, "expert", [{"material_id": "M-OP2"}], conclusion="复判春秋"
        )
        from src.relic_case.engine import AuthzError

        with self.assertRaises(AuthzError):
            self.engine.record_approval(
                self.cid, "expert", action="issue_request", approved=True
            )
        self.engine.record_approval(
            self.cid, "approver", action="issue_request", approved=True
        )
        self.assertEqual(self.engine.get_state(self.cid).stage, "attribution")


if __name__ == "__main__":
    unittest.main()
