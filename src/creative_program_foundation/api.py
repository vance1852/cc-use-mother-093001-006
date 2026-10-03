"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .allocation import AllocationService
from .errors import DomainError, ValidationError
from .service import DomainService
from .storage import Database


def _split_path(path: str) -> tuple[str, list[str]]:
    parsed = urlparse(path)
    parts = [segment for segment in parsed.path.split("/") if segment]
    return parsed, parts


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed, parts = _split_path(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            return _receipt(service.register_organization(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/actors":
            return _receipt(service.register_actor(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/sites":
            return _receipt(service.register_site(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/domain-records":
            return _receipt(service.record_domain_data(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        result = _route_allocation(service, method, parsed, parts, body, actor_id)
        if result is not None:
            return result
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, KeyError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _receipt(receipt):
    """把幂等回执与首次执行时保存的业务响应合并为 HTTP 载荷。"""

    payload = {**(receipt.response or {}), "request_id": receipt.request_id,
               "resource_type": receipt.resource_type, "resource_id": receipt.resource_id,
               "replayed": receipt.replayed}
    return 200 if receipt.replayed else 201, payload


def _route_allocation(service, method, parsed, parts, body, actor_id):
    """获奖服务资源分配相关路由；服务未启用分配能力时返回 None。"""

    if not hasattr(service, "submit_application"):
        return None
    if method == "POST" and parts == ["teams"]:
        return _receipt(service.register_team(actor_id=actor_id, **body))
    if method == "POST" and parts == ["team-relations"]:
        return _receipt(service.relate_teams(actor_id=actor_id, **body))
    if method == "POST" and parts == ["providers"]:
        return _receipt(service.register_provider(actor_id=actor_id, **body))
    if method == "POST" and parts == ["awards"]:
        return _receipt(service.register_award(actor_id=actor_id, **body))
    if method == "POST" and parts == ["resources"]:
        return _receipt(service.register_resource(actor_id=actor_id, **body))
    if method == "POST" and parts == ["windows"]:
        return _receipt(service.register_window(actor_id=actor_id, **body))
    if method == "GET" and parts == ["windows"]:
        return 200, {"items": service.list_windows(actor_id=actor_id or None)}
    if method == "POST" and parts == ["policies"]:
        return _receipt(service.create_policy(actor_id=actor_id, **body))
    if method == "GET" and parts == ["policies"]:
        return 200, {"items": service.list_policies()}
    if method == "POST" and parts == ["applications"]:
        receipt = service.submit_application(actor_id=actor_id, **body)
        status, payload = _receipt(receipt)
        payload.setdefault("application_id", receipt.resource_id)
        return status, payload
    if method == "GET" and len(parts) == 2 and parts[0] == "applications":
        return 200, service.get_application(application_id=parts[1], actor_id=actor_id)
    if method == "GET" and parts == ["waitlist"]:
        query = parse_qs(parsed.query)
        window_id = query.get("window_id", [""])[0]
        if not window_id:
            raise ValidationError("window_id 不能为空")
        return 200, service.waitlist(window_id=window_id, actor_id=actor_id)
    if method == "POST" and len(parts) == 3 and parts[0] == "items" and parts[2] == "materials":
        return 200, service.supply_material(actor_id=actor_id, item_id=parts[1],
                                           code=body["code"], content=body.get("content", {}))
    if method == "POST" and len(parts) == 3 and parts[0] == "items" and parts[2] == "confirmations":
        return 200, service.confirm_party(actor_id=actor_id, item_id=parts[1], party=body["party"])
    if method == "POST" and len(parts) == 3 and parts[0] == "items" and parts[2] == "abandon":
        return _receipt(service.abandon(actor_id=actor_id, item_id=parts[1], **body))
    if method == "POST" and len(parts) == 3 and parts[0] == "items" and parts[2] == "milestones":
        return 200, service.record_milestone(actor_id=actor_id, item_id=parts[1],
                                             code=body["code"], passed=body["passed"],
                                             note=body.get("note", ""))
    if method == "GET" and len(parts) == 3 and parts[0] == "items" and parts[2] == "policy-simulation":
        query = parse_qs(parsed.query)
        version = query.get("policy_version", [""])[0]
        if not version:
            raise ValidationError("policy_version 不能为空")
        return 200, service.simulate_policy(actor_id=actor_id, item_id=parts[1], policy_version=version)
    if method == "POST" and parts == ["fulfillments"]:
        return _receipt(service.record_fulfillment(actor_id=actor_id, **body))
    if method == "POST" and len(parts) == 3 and parts[0] == "windows" and parts[2] == "capacity":
        return _receipt(service.adjust_capacity(actor_id=actor_id, window_id=parts[1],
                                                delta=body["delta"], reason=body["reason"],
                                                request_id=body["request_id"]))
    if method == "POST" and parts == ["overrides"]:
        return 201, service.propose_override(actor_id=actor_id, item_id=body["item_id"],
                                             reason=body["reason"])
    if method == "POST" and len(parts) == 3 and parts[0] == "overrides" and parts[2] == "approve":
        return 200, service.approve_override(actor_id=actor_id, proposal_id=parts[1])
    if method == "POST" and parts == ["sweep"]:
        return 200, service.sweep_due(actor_id=actor_id)
    if method == "GET" and parts == ["provider-deliveries"]:
        return 200, service.provider_deliveries(actor_id=actor_id)
    return None


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = AllocationService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
