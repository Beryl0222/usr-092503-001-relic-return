import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import helpers  # noqa: E402
from src.relic_case.errors import (  # noqa: E402
    ConflictError,
    NotFoundError,
)


class VoidTests(unittest.TestCase):
    """撤销错误录入：只撤销最新一笔录入类事件，历史完整保留。"""

    def setUp(self):
        self.service = helpers.make_service()
        self.case_id = helpers.make_case(self.service)["case_id"]

    def _last_effective_event(self):
        events = [
            e
            for e in self.service.list_events(self.case_id)
            if e["type"] != "event_voided" and e["voided_by"] is None
        ]
        return events[-1]

    def test_void_latest_material_entry(self):
        service, cid = self.service, self.case_id
        service.submit_material(
            "officer-1", cid, "seizure_record", "查扣记录", external_id="SEIZE-2026-0001"
        )
        wrong = service.submit_material(
            "officer-1", cid, "ownership_claim", "误录的权属主张"
        )
        target_id = wrong["material"]["event_id"]
        result = service.void_event("officer-1", cid, target_id, "录入对象错误")
        self.assertEqual(result["voided_event_id"], target_id)
        materials = service.get_case(cid)["materials"]
        self.assertEqual([m["kind"] for m in materials], ["seizure_record"])

        # 历史未抹去：原事件与撤销事件都在台账中
        events = service.list_events(cid)
        target = next(e for e in events if e["event_id"] == target_id)
        self.assertIsNotNone(target["voided_by"])
        void_event = next(e for e in events if e["type"] == "event_voided")
        self.assertEqual(void_event["payload"]["target_event_id"], target_id)
        self.assertEqual(void_event["payload"]["reason"], "录入对象错误")

        # 导出中该材料标记为 voided
        export = service.export_case(cid)
        voided = [m for m in export["materials"] if m["status"] == "voided"]
        self.assertEqual(len(voided), 1)
        self.assertEqual(voided[0]["material_id"], wrong["material"]["material_id"])
        self.assertEqual(export["integrity"]["voided_count"], 1)

    def test_void_only_latest_entry(self):
        service, cid = self.service, self.case_id
        first = service.submit_material("officer-1", cid, "seizure_record", "记录一")
        service.submit_material("officer-1", cid, "ownership_claim", "记录二")
        with self.assertRaises(ConflictError):
            service.void_event(
                "officer-1", cid, first["material"]["event_id"], "跳过最新撤销更早的"
            )

    def test_void_twice_fails(self):
        service, cid = self.service, self.case_id
        entry = service.submit_material("officer-1", cid, "seizure_record", "记录")
        target_id = entry["material"]["event_id"]
        service.void_event("officer-1", cid, target_id, "撤销")
        with self.assertRaises(ConflictError):
            service.void_event("officer-1", cid, target_id, "再次撤销")

    def test_void_decision_event_rejected(self):
        service, cid = self.service, self.case_id
        helpers.drive_to_evidence_sealed(service, cid)
        sealed = next(
            e for e in service.list_events(cid) if e["type"] == "evidence_sealed"
        )
        with self.assertRaises(ConflictError):
            service.void_event("officer-1", cid, sealed["event_id"], "试图撤销决定")

    def test_void_unknown_event(self):
        with self.assertRaises(NotFoundError):
            self.service.void_event("officer-1", self.case_id, "evt-missing", "撤销")

    def test_void_confirmation_rolls_back_stage_and_frees_external_id(self):
        service, cid = self.service, self.case_id
        helpers.drive_to_confirmed(service, cid)
        target = self._last_effective_event()
        self.assertEqual(target["type"], "confirmation_recorded")
        service.void_event("officer-1", cid, target["event_id"], "来文登记错误")
        status = service.get_case(cid)
        self.assertEqual(status["stage"], "requested")
        self.assertIsNone(status["chain"]["confirmation"])
        # 外部标识已释放，可重新登记同一函件
        result = service.perform(
            "officer-1",
            cid,
            "record_confirmation",
            {"external_id": "NOTE-IN-2026-0001", "summary": "对方确认函（重录）"},
        )
        self.assertFalse(result["deduplicated"])
        self.assertEqual(service.get_case(cid)["stage"], "confirmed")

    def test_void_handover_rolls_back_to_confirmed(self):
        service, cid = self.service, self.case_id
        helpers.drive_to_handover(service, cid)
        target = self._last_effective_event()
        self.assertEqual(target["type"], "handover_recorded")
        service.void_event("officer-1", cid, target["event_id"], "回执编号录错")
        self.assertEqual(service.get_case(cid)["stage"], "confirmed")

    def test_void_appraisal_clears_pending(self):
        service, cid = self.service, self.case_id
        helpers.drive_to_evidence_sealed(service, cid)
        service.perform("expert-1", cid, "submit_appraisal", {"conclusion": "待定"})
        target = self._last_effective_event()
        service.void_event("officer-1", cid, target["event_id"], "鉴定意见录错")
        service.perform("expert-1", cid, "submit_appraisal", {"conclusion": "西周"})
        opinions = [
            m
            for m in service.export_case(cid)["materials"]
            if m["kind"] == "expert_opinion"
        ]
        # 版本号单调递增，不因撤销复用
        self.assertEqual([m["version"] for m in opinions], [1, 2])
        self.assertEqual(
            {m["status"] for m in opinions}, {"voided", "active"}
        )


if __name__ == "__main__":
    unittest.main()
