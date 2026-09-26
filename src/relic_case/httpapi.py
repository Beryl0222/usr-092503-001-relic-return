"""HTTP JSON 接口（标准库 http.server，无第三方依赖）。

鉴权约定：每个请求携带 `X-Actor-Id` 头标识操作者；其职责取自用户目录。
幂等约定：写请求可携带 `Idempotency-Key` 头（或请求体同名字段），
同一案件下相同键的重试不会二次推进。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .contracts import validate_external_id
from .engine import (
    AuthzError,
    CaseEngine,
    DomainError,
    DuplicateEventError,
    build_ledger,
)
from .storage import EventStore

IDEMPOTENCY_HEADER = "Idempotency-Key"
ACTOR_HEADER = "X-Actor-Id"


def create_app(engine: CaseEngine):
    """返回绑定到给定引擎的 Handler 类。"""

    class AppHandler(BaseHTTPRequestHandler):
        server_version = "RelicLedger/0.1"

        # ---- 基础收发

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise DomainError("请求体不是合法 JSON", code="bad_json")
            if not isinstance(data, dict):
                raise DomainError("请求体必须是 JSON 对象", code="bad_json")
            return data

        def _send_json(self, status: int, body: dict) -> None:
            encoded = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _error(self, exc: DomainError) -> None:
            detail = {"code": exc.code, "message": str(exc)}
            if isinstance(exc, DuplicateEventError):
                detail["original_event_seq"] = exc.original_seq
                if exc.replayed:
                    # 同一幂等键的网络重试：回放首次结果，不二次推进
                    self._send_json(
                        200,
                        {"replayed": True, "seq": exc.original_seq, **detail},
                    )
                    return
                detail["hint"] = "外部文书已送达，流程未重复推进"
            self._send_json(exc.http_status, {"error": detail})

        def _actor(self, data: dict) -> str:
            actor = self.headers.get(ACTOR_HEADER) or data.pop("_actor_id", None)
            if not actor:
                raise AuthzError(f"缺少请求头 {ACTOR_HEADER}")
            return actor

        def _idem(self, data: dict) -> str | None:
            key = self.headers.get(IDEMPOTENCY_HEADER) or data.pop(
                "idempotency_key", None
            )
            if key:
                validate_external_id(key)
            return key

        def log_message(self, fmt, *args):  # 静默；测试环境不打印访问日志
            return

        # ---- 路由

        def do_GET(self):  # noqa: N802
            self._dispatch("GET")

        def do_POST(self):  # noqa: N802
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                segments = [s for s in path.split("/") if s]
                data = self._read_json() if method == "POST" else {}

                if method == "POST" and segments == ["v1", "users"]:
                    return self._register_user(data)
                if method == "GET" and segments == ["v1", "cases"]:
                    return self._list_cases()
                if method == "POST" and segments == ["v1", "cases"]:
                    return self._open_case(data)

                # /v1/cases/{case_id}/...
                if len(segments) >= 3 and segments[:2] == ["v1", "cases"]:
                    case_id = segments[2]
                    sub = segments[3:]
                    if method == "GET" and sub == []:
                        return self._get_case(case_id)
                    if method == "GET" and sub == ["ledger"]:
                        return self._get_ledger(case_id)
                    if method == "POST" and sub == ["materials"]:
                        return self._register_material(case_id, data)
                    if method == "POST" and sub == ["disputes"]:
                        return self._raise_dispute(case_id, data)
                    if method == "POST" and sub == ["disputes", "resolve"]:
                        return self._resolve_dispute(case_id, data)
                    if method == "POST" and sub == ["seal"]:
                        return self._seal(case_id, data)
                    if method == "POST" and sub == ["attribution"]:
                        return self._attribution(case_id, data)
                    if method == "POST" and sub == ["approvals"]:
                        return self._approval(case_id, data)
                    if method == "POST" and sub == ["request"]:
                        return self._issue_request(case_id, data)
                    if method == "POST" and sub == ["confirmation"]:
                        return self._confirmation(case_id, data)
                    if method == "POST" and sub == ["handover"]:
                        return self._handover(case_id, data)
                    if method == "POST" and sub == ["archive"]:
                        return self._archive(case_id, data)
                    if method == "POST" and sub == ["reversals"]:
                        return self._reverse(case_id, data)

                self._send_json(
                    404, {"error": {"code": "no_route", "message": f"无此路由: {path}"}}
                )
            except DomainError as exc:
                self._error(exc)
            except KeyError as exc:
                self._error(
                    DomainError(f"缺少必填字段: {exc}", code="missing_field")
                )
            except (TypeError, ValueError) as exc:
                # 枚举/外部标识/整数转换等输入校验
                self._error(DomainError(str(exc), code="invalid_input"))
            except Exception as exc:  # 防御：服务不因单请求崩溃
                import traceback

                traceback.print_exc()
                self._send_json(
                    500,
                    {"error": {"code": "internal_error", "message": repr(exc)}},
                )

        # ---- 具体处理器

        def _register_user(self, data: dict) -> None:
            record = engine.register_user(
                user_id=data["user_id"],
                agency=data.get("agency", ""),
                roles=data.get("roles", []),
            )
            self._send_json(201, {"user": record})

        def _list_cases(self) -> None:
            ids = engine.list_cases()
            self._send_json(
                200,
                {"cases": [{"case_id": i, **_summary(engine, i)} for i in ids]},
            )

        def _open_case(self, data: dict) -> None:
            actor = self._actor(data)
            idem = self._idem(data)
            result = engine.open_case(
                case_id=data.get("case_id"),
                actor_id=actor,
                item=data.get("item", {}),
                team=data.get("team"),
                idem_key=idem,
            )
            self._send_json(201, result)

        def _get_case(self, case_id: str) -> None:
            self._send_json(200, engine.case_view(case_id))

        def _get_ledger(self, case_id: str) -> None:
            state = engine.get_state(case_id)
            self._send_json(
                200,
                build_ledger(state, engine._users),  # noqa: SLF001
            )

        def _register_material(self, case_id: str, data: dict) -> None:
            actor = self._actor(data)
            idem = self._idem(data)
            result = engine.register_material(
                case_id,
                actor,
                material_id=data["material_id"],
                kind=data["kind"],
                title=data.get("title", ""),
                version=data.get("version"),
                sha256=data.get("sha256"),
                external_ref=data.get("external_ref"),
                idem_key=idem,
            )
            self._send_json(201, result)

        def _raise_dispute(self, case_id: str, data: dict) -> None:
            actor = self._actor(data)
            result = engine.raise_dispute(case_id, actor, data.get("reason", ""))
            self._send_json(201, result)

        def _resolve_dispute(self, case_id: str, data: dict) -> None:
            actor = self._actor(data)
            result = engine.resolve_dispute(case_id, actor, data.get("resolution", ""))
            self._send_json(201, result)

        def _seal(self, case_id: str, data: dict) -> None:
            actor = self._actor(data)
            result = engine.seal_evidence(
                case_id,
                actor,
                data.get("material_refs", []),
                seal_id=data["seal_id"],
                idem_key=self._idem(data),
            )
            self._send_json(201, result)

        def _attribution(self, case_id: str, data: dict) -> None:
            actor = self._actor(data)
            result = engine.record_attribution(
                case_id,
                actor,
                data.get("material_refs", []),
                conclusion=data.get("conclusion", ""),
                idem_key=self._idem(data),
            )
            self._send_json(201, result)

        def _approval(self, case_id: str, data: dict) -> None:
            actor = self._actor(data)
            result = engine.record_approval(
                case_id,
                actor,
                action=data["action"],
                approved=bool(data.get("approved")),
                note=data.get("note"),
                material_refs=data.get("material_refs"),
                idem_key=self._idem(data),
            )
            self._send_json(201, result)

        def _issue_request(self, case_id: str, data: dict) -> None:
            actor = self._actor(data)
            result = engine.issue_request(
                case_id,
                actor,
                data.get("material_refs", []),
                external_ref=data["external_ref"],
                idem_key=self._idem(data),
            )
            self._send_json(201, result)

        def _confirmation(self, case_id: str, data: dict) -> None:
            actor = self._actor(data)
            result = engine.record_confirmation(
                case_id,
                actor,
                data.get("material_refs", []),
                external_ref=data["external_ref"],
                in_reply_to=data["in_reply_to"],
                idem_key=self._idem(data),
            )
            self._send_json(201, result)

        def _handover(self, case_id: str, data: dict) -> None:
            actor = self._actor(data)
            result = engine.complete_handover(
                case_id,
                actor,
                data.get("material_refs", []),
                external_ref=data["external_ref"],
                idem_key=self._idem(data),
            )
            self._send_json(201, result)

        def _archive(self, case_id: str, data: dict) -> None:
            actor = self._actor(data)
            result = engine.archive_case(
                case_id,
                actor,
                archive_location=data.get("archive_location", ""),
                idem_key=self._idem(data),
            )
            self._send_json(201, result)

        def _reverse(self, case_id: str, data: dict) -> None:
            actor = self._actor(data)
            result = engine.reverse_entry(
                case_id,
                actor,
                target_seq=int(data["target_seq"]),
                reason=data.get("reason", ""),
            )
            self._send_json(201, result)

    return AppHandler


def _summary(engine: CaseEngine, case_id: str) -> dict:
    view = engine.case_view(case_id)
    return {
        "stage": view["stage"],
        "disputed": view["disputed"],
        "chain_complete": view["chain"]["complete"],
        "responsible_role": view["responsible"]["role"],
        "missing": [c["code"] for c in view["missing_conditions"]],
    }


def create_server(db_path: str, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """供测试与启动脚本共用：端口 0 表示由系统分配。"""
    engine = CaseEngine(EventStore(db_path))
    handler = create_app(engine)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.engine = engine  # type: ignore[attr-defined]
    httpd.db_path = db_path  # type: ignore[attr-defined]
    return httpd
