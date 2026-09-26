"""服务重启恢复未完成案件；并发批准只能有一人成功。"""

import os
import tempfile
import threading
import unittest

from src.relic_case.engine import CaseEngine, DuplicateEventError
from src.relic_case.storage import EventStore
from support import USERS, drive_to


def fresh_engine(path: str) -> CaseEngine:
    engine = CaseEngine(EventStore(path))
    # 用户目录也持久化；仅首次需要建
    for uid, (agency, roles) in USERS.items():
        try:
            engine.register_user(uid, agency, roles)
        except Exception:
            pass
    return engine


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except FileNotFoundError:
                pass

    def test_recover_inflight_case_after_restart(self):
        engine = fresh_engine(self.path)
        drive_to(engine, "requested")
        engine.store.close()

        # 模拟服务重启：新引擎从同一数据库重放
        revived = fresh_engine(self.path)
        state = revived.get_state("CASE-2026-0001")
        self.assertEqual(state.stage, "requested")
        self.assertEqual(state.request_ref, "NOTE-OUT-0001")

        view = revived.case_view("CASE-2026-0001")
        missing = {c["code"] for c in view["missing_conditions"]}
        self.assertIn("counterpart_confirmation", missing)

        # 恢复后可继续推进直到闭合
        revived.register_material(
            "CASE-2026-0001", "custodian", material_id="M-IN",
            kind="diplomatic_note", title="确认", external_ref="NOTE-IN-0001",
        )
        revived.record_confirmation(
            "CASE-2026-0001", "custodian", [{"material_id": "M-IN"}],
            external_ref="NOTE-IN-0001", in_reply_to="NOTE-OUT-0001",
        )
        revived.record_approval(
            "CASE-2026-0001", "reviewer", action="complete_handover",
            approved=True,
        )
        revived.register_material(
            "CASE-2026-0001", "custodian", material_id="M-R",
            kind="handover_receipt", title="回执", external_ref="RCPT-0001",
        )
        revived.complete_handover(
            "CASE-2026-0001", "custodian", [{"material_id": "M-R"}],
            external_ref="RCPT-0001",
        )
        revived.record_approval(
            "CASE-2026-0001", "approver", action="archive", approved=True
        )
        revived.archive_case(
            "CASE-2026-0001", "officer", archive_location="一号库"
        )
        view = revived.case_view("CASE-2026-0001")
        self.assertTrue(view["chain"]["complete"])
        revived.store.close()

    def test_recover_disputed_state(self):
        engine = fresh_engine(self.path)
        drive_to(engine, "attribution")
        engine.raise_dispute("CASE-2026-0001", "officer", "待查")
        engine.store.close()

        revived = fresh_engine(self.path)
        view = revived.case_view("CASE-2026-0001")
        self.assertTrue(view["restricted"])
        self.assertEqual(view["stage"], "attribution")
        revived.store.close()

    def test_recover_users_directory(self):
        engine = fresh_engine(self.path)
        engine.store.close()
        revived = CaseEngine(EventStore(self.path))
        self.assertIn("officer", revived._users)
        self.assertEqual(revived._users["expert"]["roles"], ["expert"])
        revived.store.close()


class ConcurrentApprovalTests(unittest.TestCase):
    def test_parallel_approval_only_one_succeeds(self):
        engine = fresh_engine(":memory:")
        drive_to(engine, "attribution")
        # 两名具备资格的批准人同时批准同一动作
        barrier = threading.Barrier(2)
        outcomes: list[Exception | dict] = []

        def approve(actor: str):
            try:
                barrier.wait()
                outcomes.append(
                    engine.record_approval(
                        "CASE-2026-0001", actor,
                        action="issue_request", approved=True,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                outcomes.append(exc)

        threads = [
            threading.Thread(target=approve, args=("approver",)),
            threading.Thread(target=approve, args=("expert_boss",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [o for o in outcomes if not isinstance(o, Exception)]
        duplicates = [o for o in outcomes if isinstance(o, DuplicateEventError)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(duplicates), 1)
        # 事件流中只有一个批准
        approvals = [
            e for e in engine.get_state("CASE-2026-0001").events
            if e["event_type"] == "approval_recorded"
        ]
        self.assertEqual(len(approvals), 1)

    def test_concurrent_handover_same_receipt_single_advance(self):
        engine = fresh_engine(":memory:")
        drive_to(engine, "requested")
        engine.register_material(
            "CASE-2026-0001", "custodian", material_id="M-IN",
            kind="diplomatic_note", title="确认", external_ref="NOTE-IN-0001",
        )
        engine.record_confirmation(
            "CASE-2026-0001", "custodian", [{"material_id": "M-IN"}],
            external_ref="NOTE-IN-0001", in_reply_to="NOTE-OUT-0001",
        )
        engine.register_material(
            "CASE-2026-0001", "custodian", material_id="M-R",
            kind="handover_receipt", title="回执", external_ref="RCPT-0001",
        )
        engine.record_approval(
            "CASE-2026-0001", "reviewer", action="complete_handover",
            approved=True,
        )

        outcomes: list[Exception | dict] = []
        barrier = threading.Barrier(2)

        def handover():
            try:
                barrier.wait()
                outcomes.append(
                    engine.complete_handover(
                        "CASE-2026-0001", "custodian",
                        [{"material_id": "M-R"}], external_ref="RCPT-0001",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                outcomes.append(exc)

        threads = [threading.Thread(target=handover) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(
            sum(not isinstance(o, Exception) for o in outcomes), 1
        )
        self.assertEqual(engine.get_state("CASE-2026-0001").stage, "handover")


if __name__ == "__main__":
    unittest.main()
