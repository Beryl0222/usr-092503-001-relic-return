import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import helpers  # noqa: E402
from src.relic_case import CaseService, EventStore  # noqa: E402


class RecoveryTests(unittest.TestCase):
    """服务重启后从持久事件恢复未完成案件。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="relic-case-")
        self.db_path = os.path.join(self.tmpdir, "ledger.db")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _reopen(self):
        """模拟重启：基于同一数据文件重建服务，不重新登记操作者。"""

        return CaseService(EventStore(self.db_path))

    def test_unfinished_case_survives_restart(self):
        service = helpers.make_service(self.db_path)
        case_id = helpers.make_case(service)["case_id"]
        helpers.drive_to_requested(service, case_id)
        before = service.get_case(case_id)
        service.close()

        restored = self._reopen()
        after = restored.get_case(case_id)
        self.assertEqual(after["stage"], "requested")
        self.assertEqual(after["title"], before["title"])
        self.assertEqual(after["relic"], before["relic"])
        self.assertEqual(
            [m["material_id"] for m in after["materials"]],
            [m["material_id"] for m in before["materials"]],
        )
        self.assertEqual(
            after["chain"]["request"]["external_id"], "NOTE-OUT-2026-0001"
        )
        # 操作者名册同样恢复，无需重新登记即可继续流程
        restored.perform(
            "officer-1",
            case_id,
            "record_confirmation",
            {"external_id": "NOTE-IN-2026-0001"},
        )
        restored.perform(
            "custodian-1",
            case_id,
            "record_handover",
            {"external_id": "RCPT-2026-0001"},
        )
        restored.perform("approver-1", case_id, "close_case")
        self.assertTrue(restored.get_case(case_id)["handover_chain_closed"])
        restored.close()

        again = self._reopen()
        self.assertEqual(again.get_case(case_id)["stage"], "archived")
        again.close()

    def test_event_log_and_export_survive_restart(self):
        service = helpers.make_service(self.db_path)
        case_id = helpers.make_case(service)["case_id"]
        helpers.drive_to_confirmed(service, case_id)
        events_before = service.list_events(case_id)
        export_before = service.export_case(case_id)
        service.close()

        restored = self._reopen()
        events_after = restored.list_events(case_id)
        self.assertEqual(
            [e["event_id"] for e in events_after],
            [e["event_id"] for e in events_before],
        )
        export_after = restored.export_case(case_id)
        self.assertEqual(
            export_after["integrity"]["event_count"],
            export_before["integrity"]["event_count"],
        )
        self.assertEqual(
            export_after["chain_of_custody"]["confirmation"]["external_id"],
            "NOTE-IN-2026-0001",
        )
        restored.close()

    def test_void_state_survives_restart(self):
        service = helpers.make_service(self.db_path)
        case_id = helpers.make_case(service)["case_id"]
        entry = service.submit_material(
            "officer-1", case_id, "seizure_record", "误录材料"
        )
        service.void_event(
            "officer-1", case_id, entry["material"]["event_id"], "录入错误"
        )
        service.close()

        restored = self._reopen()
        self.assertEqual(restored.get_case(case_id)["materials"], [])
        events = restored.list_events(case_id)
        self.assertEqual(
            [e["type"] for e in events],
            ["case_created", "material_submitted", "event_voided"],
        )
        self.assertIsNotNone(events[1]["voided_by"])
        export = restored.export_case(case_id)
        self.assertEqual(export["materials"][0]["status"], "voided")
        self.assertEqual(export["integrity"]["voided_count"], 1)
        restored.close()

    def test_hold_state_survives_restart(self):
        service = helpers.make_service(self.db_path)
        case_id = helpers.make_case(service)["case_id"]
        helpers.drive_to_evidence_sealed(service, case_id)
        service.perform("officer-1", case_id, "raise_dispute", {"reason": "争议"})
        service.close()

        restored = self._reopen()
        status = restored.get_case(case_id)
        self.assertTrue(status["held"])
        self.assertEqual(status["hold_reason"], "争议")
        from src.relic_case.errors import OnHoldError

        with self.assertRaises(OnHoldError):
            restored.perform(
                "expert-1", case_id, "submit_appraisal", {"conclusion": "西周"}
            )
        restored.close()


if __name__ == "__main__":
    unittest.main()
