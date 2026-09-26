import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import helpers  # noqa: E402
from src.relic_case import make_server  # noqa: E402


def eid(prefix):
    """生成符合格式且全局唯一的外部标识。"""

    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class ApiTests(unittest.TestCase):
    """HTTP JSON 接口端到端：各机构系统真实调用。"""

    @classmethod
    def setUpClass(cls):
        cls.service = helpers.make_service()
        cls.server = make_server(cls.service, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.service.close()
        cls.thread.join(timeout=5)

    def call(self, method, path, body=None, actor=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if actor is not None:
            request.add_header("X-Actor-Id", actor)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def create_case(self):
        status, payload = self.call(
            "POST",
            "/cases",
            {"title": "唐代三彩返还案", "relic": {"name": "三彩马", "serial": "TS-007"}},
            actor="officer-1",
        )
        self.assertEqual(status, 201)
        return payload["case"]["case_id"]

    def test_health(self):
        status, payload = self.call("GET", "/health")
        self.assertEqual((status, payload["status"]), (200, "ok"))

    def test_full_flow_over_http(self):
        cid = self.create_case()

        status, payload = self.call(
            "POST",
            f"/cases/{cid}/materials",
            {"kind": "seizure_record", "summary": "口岸查扣记录", "external_id": eid("SEIZE")},
            actor="officer-1",
        )
        self.assertEqual(status, 201)
        self.assertFalse(payload["deduplicated"])

        for actor, action, body in [
            ("officer-1", "seal_evidence", {}),
            ("expert-1", "submit_appraisal", {"conclusion": "唐代三彩"}),
            ("reviewer-1", "approve_attribution", {}),
            ("officer-1", "draft_request", {"summary": "返还请求函", "external_id": eid("NOTE-OUT")}),
            ("approver-1", "approve_request", {}),
            ("officer-1", "record_confirmation", {"external_id": eid("NOTE-IN")}),
            ("custodian-1", "record_handover", {"external_id": eid("RCPT")}),
            ("approver-1", "close_case", {}),
        ]:
            status, payload = self.call(
                "POST", f"/cases/{cid}/actions/{action}", body, actor=actor
            )
            self.assertEqual(status, 200, f"{action}: {payload}")

        status, payload = self.call("GET", f"/cases/{cid}")
        self.assertEqual(status, 200)
        case = payload["case"]
        self.assertEqual(case["stage"], "archived")
        self.assertTrue(case["handover_chain_closed"])
        self.assertEqual(case["pending_conditions"], [])

        status, export = self.call("GET", f"/cases/{cid}/export")
        self.assertEqual(status, 200)
        self.assertEqual(export["export_type"], "relic_case_regulatory_record")
        self.assertTrue(export["chain_of_custody"]["closed"])
        self.assertGreaterEqual(export["integrity"]["event_count"], 9)
        self.assertIn("officer-1", export["actors"])

    def test_status_shows_responsible_and_missing_conditions(self):
        cid = self.create_case()
        status, payload = self.call("GET", f"/cases/{cid}")
        self.assertEqual(status, 200)
        case = payload["case"]
        self.assertEqual(case["responsible"]["role"], "case_officer")
        codes = {c["code"] for c in case["pending_conditions"]}
        self.assertIn("need_seizure_record", codes)
        self.assertFalse(case["handover_chain_closed"])

    def test_unknown_actor_gets_401(self):
        cid = self.create_case()
        status, payload = self.call(
            "POST", f"/cases/{cid}/actions/seal_evidence", {}, actor="nobody"
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unknown_actor")

    def test_missing_actor_header_gets_401(self):
        cid = self.create_case()
        status, payload = self.call("POST", f"/cases/{cid}/actions/seal_evidence", {})
        self.assertEqual(status, 401)

    def test_forbidden_role_gets_403(self):
        cid = self.create_case()
        status, payload = self.call(
            "POST", f"/cases/{cid}/actions/seal_evidence", {}, actor="expert-1"
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "forbidden")

    def test_out_of_order_gets_409(self):
        cid = self.create_case()
        status, payload = self.call(
            "POST",
            f"/cases/{cid}/actions/record_confirmation",
            {"external_id": eid("NOTE-IN")},
            actor="officer-1",
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "invalid_transition")

    def test_unknown_case_gets_404(self):
        status, payload = self.call("GET", "/cases/case-missing00")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_unknown_route_gets_404(self):
        status, payload = self.call("GET", "/nope")
        self.assertEqual(status, 404)

    def test_bad_json_gets_400(self):
        request = urllib.request.Request(
            f"{self.base}/cases", data=b"{not json", method="POST"
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Actor-Id", "officer-1")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                self.fail(f"应返回 400，实际 {response.status}")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            payload = json.loads(exc.read().decode("utf-8"))
            self.assertEqual(payload["error"]["code"], "validation")

    def test_duplicate_confirmation_over_http(self):
        cid = self.create_case()
        note_in = eid("NOTE-IN")
        self.call(
            "POST",
            f"/cases/{cid}/materials",
            {"kind": "seizure_record", "summary": "查扣记录", "external_id": eid("SEIZE")},
            actor="officer-1",
        )
        for actor, action, body in [
            ("officer-1", "seal_evidence", {}),
            ("expert-1", "submit_appraisal", {"conclusion": "唐代"}),
            ("reviewer-1", "approve_attribution", {}),
            ("officer-1", "draft_request", {"summary": "请求函"}),
            ("approver-1", "approve_request", {}),
        ]:
            self.call("POST", f"/cases/{cid}/actions/{action}", body, actor=actor)

        first_status, first = self.call(
            "POST",
            f"/cases/{cid}/actions/record_confirmation",
            {"external_id": note_in},
            actor="officer-1",
        )
        second_status, second = self.call(
            "POST",
            f"/cases/{cid}/actions/record_confirmation",
            {"external_id": note_in},
            actor="officer-1",
        )
        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["event_id"], first["event_id"])

        status, payload = self.call("GET", f"/cases/{cid}/events")
        confirmations = [
            e for e in payload["events"] if e["type"] == "confirmation_recorded"
        ]
        self.assertEqual(len(confirmations), 1)

    def test_void_over_http(self):
        cid = self.create_case()
        _, material = self.call(
            "POST",
            f"/cases/{cid}/materials",
            {"kind": "ownership_claim", "summary": "误录主张"},
            actor="officer-1",
        )
        _, events = self.call("GET", f"/cases/{cid}/events")
        target = events["events"][-1]
        status, payload = self.call(
            "POST",
            f"/cases/{cid}/void",
            {"event_id": target["event_id"], "reason": "录入错误"},
            actor="officer-1",
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["voided_event_id"], target["event_id"])
        _, case = self.call("GET", f"/cases/{cid}")
        self.assertEqual(case["case"]["materials"], [])

    def test_list_cases_summary(self):
        cid = self.create_case()
        status, payload = self.call("GET", "/cases")
        self.assertEqual(status, 200)
        mine = next(c for c in payload["cases"] if c["case_id"] == cid)
        self.assertEqual(mine["stage"], "intake")
        self.assertIn("responsible", mine)

    def test_register_actor_over_http(self):
        actor_id = f"liaison-{uuid.uuid4().hex[:8]}"
        status, payload = self.call(
            "POST",
            "/actors",
            {
                "actor_id": actor_id,
                "name": "联络员",
                "org": "海关",
                "roles": ["case_officer"],
            },
        )
        self.assertEqual(status, 201)
        again, payload2 = self.call(
            "POST",
            "/actors",
            {
                "actor_id": actor_id,
                "name": "联络员",
                "org": "海关",
                "roles": ["case_officer"],
            },
        )
        self.assertEqual(again, 200)
        self.assertTrue(payload2["deduplicated"])


if __name__ == "__main__":
    unittest.main()
