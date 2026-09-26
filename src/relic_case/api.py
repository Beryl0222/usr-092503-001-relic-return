"""HTTP JSON 接口：供各机构系统调用。

约定：
- 变更类请求必须携带 X-Actor-Id 头，操作者须已登记。
- 成功响应为 JSON；错误响应为 {"error": {"code", "message"}}。
- 外部函件/回执重复送达返回 200 且 deduplicated=true，不重复推进。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import DomainError, NotFoundError, UnknownActorError, ValidationError
from .service import CaseService


class RelicRequestHandler(BaseHTTPRequestHandler):
    server_version = "RelicCase/0.1"
    protocol_version = "HTTP/1.1"

    @property
    def service(self) -> CaseService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, *args) -> None:  # 保持静默，调用方自行记录访问日志
        pass

    # ------------------------------------------------------------------
    # 基础读写
    # ------------------------------------------------------------------

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValidationError("请求体不是合法的 JSON") from None
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return data

    def _actor_id(self) -> str:
        actor_id = self.headers.get("X-Actor-Id")
        if not actor_id:
            raise UnknownActorError("缺少 X-Actor-Id 请求头")
        return actor_id

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            status, payload = self._route(method)
            self._send_json(status, payload)
        except DomainError as exc:
            self._send_json(
                exc.status, {"error": {"code": exc.code, "message": exc.message}}
            )
        except Exception as exc:  # 兜底，避免连接悬挂
            self._send_json(
                500, {"error": {"code": "internal", "message": f"服务内部错误: {exc}"}}
            )

    def _route(self, method: str) -> tuple[int, dict]:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if method == "GET" and path == "/health":
            return 200, {"status": "ok"}

        if method == "POST" and path == "/actors":
            body = self._read_json()
            result = self.service.register_actor(
                body.get("actor_id"),
                body.get("name"),
                body.get("org"),
                body.get("roles") or [],
            )
            return (200 if result["deduplicated"] else 201), result

        if method == "GET" and path == "/actors":
            return 200, {"actors": self.service.list_actors()}

        if method == "POST" and path == "/cases":
            body = self._read_json()
            case = self.service.create_case(
                self._actor_id(),
                body.get("title"),
                body.get("relic"),
                body.get("case_id"),
            )
            return 201, {"case": case}

        if method == "GET" and path == "/cases":
            return 200, {"cases": self.service.list_cases()}

        match = re.fullmatch(r"/cases/([^/]+)", path)
        if match and method == "GET":
            return 200, {"case": self.service.get_case(match.group(1))}

        match = re.fullmatch(r"/cases/([^/]+)/events", path)
        if match and method == "GET":
            return 200, {"events": self.service.list_events(match.group(1))}

        match = re.fullmatch(r"/cases/([^/]+)/export", path)
        if match and method == "GET":
            return 200, self.service.export_case(match.group(1))

        match = re.fullmatch(r"/cases/([^/]+)/materials", path)
        if match and method == "POST":
            body = self._read_json()
            result = self.service.submit_material(
                self._actor_id(),
                match.group(1),
                body.get("kind"),
                body.get("summary"),
                body.get("uri"),
                body.get("external_id"),
            )
            return (200 if result["deduplicated"] else 201), result

        match = re.fullmatch(r"/cases/([^/]+)/actions/([^/]+)", path)
        if match and method == "POST":
            result = self.service.perform(
                self._actor_id(), match.group(1), match.group(2), self._read_json()
            )
            return 200, result

        match = re.fullmatch(r"/cases/([^/]+)/void", path)
        if match and method == "POST":
            body = self._read_json()
            result = self.service.void_event(
                self._actor_id(),
                match.group(1),
                body.get("event_id"),
                body.get("reason"),
            )
            return 200, result

        raise NotFoundError(f"未知路径: {method} {path}")


def make_server(
    service: CaseService, host: str = "127.0.0.1", port: int = 8080
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), RelicRequestHandler)
    server.service = service  # type: ignore[attr-defined]
    server.daemon_threads = True
    return server
