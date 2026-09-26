"""端到端 HTTP JSON 接口测试：鉴权头、幂等头、错误码与闭合链路。"""

import json
import threading
import unittest
import urllib.error
import urllib.request

from src.relic_case.httpapi import create_server
from support import USERS


class HttpCase:
    def __init__(self, url: str, method: str, path: str, body=None,
                 actor=None, idem=None):
        self.url = url + path
        self.method = method
        self.body = body
        self.actor = actor
        self.idem = idem


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = create_server(":memory:", port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.engine.store.close()

    def call(self, method, path, body=None, actor=None, idem=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        if actor:
            req.add_header("X-Actor-Id", actor)
        if idem:
            req.add_header("Idempotency-Key", idem)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def setUp(self):
        for uid, (agency, roles) in USERS.items():
            status, body = self.call(
                "POST", "/v1/users",
                {"user_id": uid, "agency": agency, "roles": roles},
            )
            self.assertIn(status, (201, 400), body)

    def _open_case(self, cid="CASE-HTTP-0001"):
        status, body = self.call(
            "POST", "/v1/cases",
            {"case_id": cid, "item": {"name": "HTTP 青铜鼎"},
             "team": {"case_officer_id": "officer", "expert_ids": ["expert"],
                      "approver_ids": ["approver"],
                      "reviewer_ids": ["reviewer"],
                      "custodian_id": "custodian"}},
            actor="officer",
        )
        self.assertEqual(status, 201, body)
        return cid

    def _drive(self, cid):
        # intake → archived 全链路经 HTTP 调用
        self.call("POST", f"/v1/cases/{cid}/materials",
                  {"material_id": "M-S", "kind": "seizure_record",
                   "title": "查获记录", "sha256": "a" * 64}, actor="officer")
        self.call("POST", f"/v1/cases/{cid}/seal",
                  {"material_refs": [{"material_id": "M-S"}],
                   "seal_id": "SEAL-1"}, actor="officer")
        self.call("POST", f"/v1/cases/{cid}/materials",
                  {"material_id": "M-O", "kind": "expert_opinion",
                   "title": "鉴定书"}, actor="expert")
        self.call("POST", f"/v1/cases/{cid}/attribution",
                  {"material_refs": [{"material_id": "M-O"}],
                   "conclusion": "春秋"}, actor="expert")
        self.call("POST", f"/v1/cases/{cid}/approvals",
                  {"action": "issue_request", "approved": True},
                  actor="approver")
        self.call("POST", f"/v1/cases/{cid}/materials",
                  {"material_id": "M-NO", "kind": "diplomatic_note",
                   "title": "请求函", "external_ref": "HNOTE-OUT-1"},
                  actor="officer")
        self.call("POST", f"/v1/cases/{cid}/request",
                  {"material_refs": [{"material_id": "M-NO"}],
                   "external_ref": "HNOTE-OUT-1"}, actor="officer")
        self.call("POST", f"/v1/cases/{cid}/materials",
                  {"material_id": "M-NI", "kind": "diplomatic_note",
                   "title": "确认函", "external_ref": "HNOTE-IN-1"},
                  actor="custodian")
        self.call("POST", f"/v1/cases/{cid}/confirmation",
                  {"material_refs": [{"material_id": "M-NI"}],
                   "external_ref": "HNOTE-IN-1",
                   "in_reply_to": "HNOTE-OUT-1"}, actor="custodian")
        self.call("POST", f"/v1/cases/{cid}/approvals",
                  {"action": "complete_handover", "approved": True},
                  actor="reviewer")
        self.call("POST", f"/v1/cases/{cid}/materials",
                  {"material_id": "M-R", "kind": "handover_receipt",
                   "title": "回执", "external_ref": "HRCPT-0001"},
                  actor="custodian")
        self.call("POST", f"/v1/cases/{cid}/handover",
                  {"material_refs": [{"material_id": "M-R"}],
                   "external_ref": "HRCPT-0001"}, actor="custodian")
        self.call("POST", f"/v1/cases/{cid}/approvals",
                  {"action": "archive", "approved": True}, actor="approver")
        self.call("POST", f"/v1/cases/{cid}/archive",
                  {"archive_location": "一号库"}, actor="officer")

    def test_full_chain_over_http(self):
        cid = self._open_case()
        self._drive(cid)
        status, view = self.call("GET", f"/v1/cases/{cid}")
        self.assertEqual(status, 200)
        self.assertEqual(view["stage"], "archived")
        self.assertTrue(view["chain"]["complete"])
        self.assertEqual(view["missing_conditions"], [])

    def test_missing_actor_header_is_403(self):
        status, body = self.call(
            "POST", "/v1/cases", {"item": {"name": "x"}}
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

    def test_out_of_order_is_409(self):
        cid = self._open_case("CASE-HTTP-0002")
        status, body = self.call(
            "POST", f"/v1/cases/{cid}/attribution",
            {"material_refs": [], "conclusion": "跳步"}, actor="expert",
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "out_of_order")

    def test_forbidden_is_403(self):
        cid = self._open_case("CASE-HTTP-0003")
        status, body = self.call(
            "POST", f"/v1/cases/{cid}/seal",
            {"material_refs": [], "seal_id": "S"}, actor="custodian",
        )
        self.assertEqual(status, 403)

    def test_duplicate_external_note_is_409_and_points_to_original(self):
        cid = self._open_case("CASE-HTTP-0004")
        self.call("POST", f"/v1/cases/{cid}/materials",
                  {"material_id": "M-S", "kind": "seizure_record",
                   "title": "r"}, actor="officer")
        self.call("POST", f"/v1/cases/{cid}/seal",
                  {"material_refs": [{"material_id": "M-S"}], "seal_id": "S1"},
                  actor="officer")
        self.call("POST", f"/v1/cases/{cid}/materials",
                  {"material_id": "M-O", "kind": "expert_opinion",
                   "title": "o"}, actor="expert")
        self.call("POST", f"/v1/cases/{cid}/attribution",
                  {"material_refs": [{"material_id": "M-O"}],
                   "conclusion": "c"}, actor="expert")
        self.call("POST", f"/v1/cases/{cid}/approvals",
                  {"action": "issue_request", "approved": True},
                  actor="approver")
        self.call("POST", f"/v1/cases/{cid}/materials",
                  {"material_id": "M-NO", "kind": "diplomatic_note",
                   "title": "out", "external_ref": "DUPNOTE-1"},
                  actor="officer")
        self.call("POST", f"/v1/cases/{cid}/request",
                  {"material_refs": [{"material_id": "M-NO"}],
                   "external_ref": "DUPNOTE-1"}, actor="officer")
        # 重发同文号请求函
        status, body = self.call(
            "POST", f"/v1/cases/{cid}/request",
            {"material_refs": [{"material_id": "M-NO"}],
             "external_ref": "DUPNOTE-1"}, actor="officer",
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "duplicate_event")
        self.assertIn("original_event_seq", body["error"])

    def test_idempotent_retry_returns_200_replayed(self):
        cid = self._open_case("CASE-HTTP-0005")
        payload = {"material_id": "M-S", "kind": "seizure_record", "title": "r"}
        s1, b1 = self.call("POST", f"/v1/cases/{cid}/materials", payload,
                           actor="officer", idem="HTTP-IDEM-0001")
        s2, b2 = self.call("POST", f"/v1/cases/{cid}/materials", payload,
                           actor="officer", idem="HTTP-IDEM-0001")
        self.assertEqual(s1, 201)
        self.assertEqual(s2, 200)
        self.assertTrue(b2["replayed"])
        self.assertEqual(b1["seq"], b2["seq"])

    def test_ledger_export_is_complete(self):
        cid = self._open_case("CASE-HTTP-0006")
        self._drive(cid)
        status, ledger = self.call("GET", f"/v1/cases/{cid}/ledger")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(ledger["event_log"]), 13)
        self.assertEqual(ledger["event_log"][0]["event_type"], "case_opened")
        # 每个关键决定都含操作者、时间、材料版本依据
        for d in ledger["decisions"]:
            self.assertTrue(d["actor_id"])
            self.assertTrue(d["decided_at"])
        # 列表接口反映闭合状态
        status, listing = self.call("GET", "/v1/cases")
        entry = next(c for c in listing["cases"] if c["case_id"] == cid)
        self.assertTrue(entry["chain_complete"])

    def test_case_status_shows_responsible_and_missing(self):
        cid = self._open_case("CASE-HTTP-0007")
        status, view = self.call("GET", f"/v1/cases/{cid}")
        self.assertEqual(view["responsible"]["role"], "case_officer")
        codes = {c["code"] for c in view["missing_conditions"]}
        self.assertEqual(codes, {"seizure_record", "seal_evidence"})

    def test_dispute_restricts_and_ledger_shows_it(self):
        cid = self._open_case("CASE-HTTP-0008")
        status, _ = self.call(
            "POST", f"/v1/cases/{cid}/disputes",
            {"reason": "权属争议"}, actor="officer",
        )
        self.assertEqual(status, 201)
        status, view = self.call("GET", f"/v1/cases/{cid}")
        self.assertTrue(view["restricted"])
        status, body = self.call(
            "POST", f"/v1/cases/{cid}/materials",
            {"material_id": "M-S", "kind": "seizure_record", "title": "r"},
            actor="officer",
        )
        self.assertEqual(status, 201)  # 材料登记允许
        status, body = self.call(
            "POST", f"/v1/cases/{cid}/seal",
            {"material_refs": [{"material_id": "M-S"}], "seal_id": "S"},
            actor="officer",
        )
        self.assertEqual(status, 409)  # 推进被冻结

    def test_unknown_case_404(self):
        status, body = self.call("GET", "/v1/cases/CASE-NOPE-9999")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_reversal_keeps_history(self):
        cid = self._open_case("CASE-HTTP-0009")
        _, mat = self.call(
            "POST", f"/v1/cases/{cid}/materials",
            {"material_id": "M-TYPO", "kind": "seizure_record", "title": "错"},
            actor="officer",
        )
        status, body = self.call(
            "POST", f"/v1/cases/{cid}/reversals",
            {"target_seq": mat["seq"], "reason": "类别录错"},
            actor="reviewer",
        )
        self.assertEqual(status, 201, body)
        _, ledger = self.call("GET", f"/v1/cases/{cid}/ledger")
        seqs = [e["seq"] for e in ledger["event_log"]]
        self.assertIn(mat["seq"], seqs)  # 原事件仍在
        self.assertIn(body["seq"], seqs)


if __name__ == "__main__":
    unittest.main()
