import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import helpers  # noqa: E402
from src.relic_case.errors import DomainError  # noqa: E402


def run_concurrently(fn, workers=8):
    """并发执行 fn，返回 (成功结果列表, 异常列表)。"""

    def call(_):
        try:
            return ("ok", fn())
        except DomainError as exc:
            return ("err", exc)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(call, range(workers)))
    return (
        [item for kind, item in outcomes if kind == "ok"],
        [item for kind, item in outcomes if kind == "err"],
    )


class ConcurrencyTests(unittest.TestCase):
    """并发批准与并发重复送达：状态只推进一次。"""

    def setUp(self):
        self.service = helpers.make_service()
        self.case_id = helpers.make_case(self.service)["case_id"]

    def test_concurrent_approve_attribution_single_winner(self):
        service, cid = self.service, self.case_id
        helpers.drive_to_evidence_sealed(service, cid)
        service.perform("expert-1", cid, "submit_appraisal", {"conclusion": "西周"})

        successes, errors = run_concurrently(
            lambda: service.perform("reviewer-1", cid, "approve_attribution")
        )
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(errors), 7)
        approved = [
            e
            for e in service.list_events(cid)
            if e["type"] == "attribution_approved"
        ]
        self.assertEqual(len(approved), 1)
        self.assertEqual(service.get_case(cid)["stage"], "attribution")

    def test_concurrent_approve_request_single_winner(self):
        service, cid = self.service, self.case_id
        helpers.drive_to_attribution(service, cid)
        service.perform("officer-1", cid, "draft_request", {"summary": "请求函"})

        successes, errors = run_concurrently(
            lambda: service.perform("approver-1", cid, "approve_request")
        )
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(errors), 7)
        approved = [
            e for e in service.list_events(cid) if e["type"] == "request_approved"
        ]
        self.assertEqual(len(approved), 1)
        self.assertEqual(service.get_case(cid)["stage"], "requested")

    def test_concurrent_duplicate_confirmation_applied_once(self):
        service, cid = self.service, self.case_id
        helpers.drive_to_requested(service, cid)

        successes, errors = run_concurrently(
            lambda: service.perform(
                "officer-1",
                cid,
                "record_confirmation",
                {"external_id": "NOTE-IN-2026-0001"},
            )
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(successes), 8)
        fresh = [s for s in successes if not s["deduplicated"]]
        self.assertEqual(len(fresh), 1)
        confirmations = [
            e
            for e in service.list_events(cid)
            if e["type"] == "confirmation_recorded"
        ]
        self.assertEqual(len(confirmations), 1)
        self.assertEqual(service.get_case(cid)["stage"], "confirmed")

    def test_concurrent_close_case_single_winner(self):
        service, cid = self.service, self.case_id
        helpers.drive_to_handover(service, cid)

        successes, errors = run_concurrently(
            lambda: service.perform("approver-1", cid, "close_case")
        )
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(errors), 7)
        closed = [e for e in service.list_events(cid) if e["type"] == "case_closed"]
        self.assertEqual(len(closed), 1)
        self.assertTrue(service.get_case(cid)["handover_chain_closed"])


if __name__ == "__main__":
    unittest.main()
