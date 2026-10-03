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


def _allocation_route(alloc: AllocationService, method: str, parts: list[str],
                      query: dict[str, list[str]], body: dict[str, Any],
                      actor_id: str) -> tuple[int, Any]:
    """处理 /allocation/* 路由，parts 不含前导 allocation 段。"""

    if method == "POST" and parts == ["teams"]:
        result = alloc.register_team(actor_id=actor_id, **body)
        return 200 if result.get("replayed") else 201, result
    if method == "POST" and parts == ["providers"]:
        result = alloc.register_provider(actor_id=actor_id, **body)
        return 200 if result.get("replayed") else 201, result
    if method == "POST" and parts == ["projects"]:
        result = alloc.register_project(actor_id=actor_id, **body)
        return 200 if result.get("replayed") else 201, result
    if method == "POST" and parts == ["contacts"]:
        result = alloc.register_contact(actor_id=actor_id, **body)
        return 200 if result.get("replayed") else 201, result
    if method == "POST" and parts == ["resources"]:
        result = alloc.register_resource(actor_id=actor_id, **body)
        return 200 if result.get("replayed") else 201, result
    if method == "POST" and parts == ["windows"]:
        result = alloc.add_window(actor_id=actor_id, **body)
        return 200 if result.get("replayed") else 201, result
    if method == "POST" and parts == ["milestones"]:
        result = alloc.add_milestone(actor_id=actor_id, **body)
        return 200 if result.get("replayed") else 201, result
    if method == "POST" and parts == ["policies"]:
        result = alloc.create_policy(actor_id=actor_id, **body)
        return 200 if result.get("replayed") else 201, result
    if method == "GET" and parts == ["policies"]:
        return 200, {"items": alloc.list_policies()}
    if method == "GET" and parts == ["policy-comparison"]:
        return 200, alloc.policy_comparison()
    if method == "POST" and parts == ["applications"]:
        result = alloc.submit_application(actor_id=actor_id, **body)
        return 200 if result.get("replayed") else 201, result
    if method == "POST" and parts == ["due-processing"]:
        return 200, alloc.run_due_processing()
    if method == "POST" and parts == ["capacity-reductions"]:
        result = alloc.reduce_window_capacity(actor_id=actor_id, **body)
        return 200 if result.get("replayed") else 201, result
    if method == "POST" and parts == ["overrides"]:
        result = alloc.propose_override(actor_id=actor_id, **body)
        return 200 if result.get("replayed") else 201, result

    # /applications/{id}/...
    if len(parts) >= 2 and parts[0] == "applications":
        application_id = parts[1]
        if method == "GET" and len(parts) == 2:
            return 200, alloc.get_application(application_id)
        if len(parts) >= 4 and parts[2] == "lines":
            line_id = parts[3]
            if method == "POST" and len(parts) == 5 and parts[4] == "materials":
                return 201, alloc.add_material(actor_id=actor_id, application_id=application_id,
                                               line_id=line_id, kind=body["kind"],
                                               document_ref=body["document_ref"])
            if method == "POST" and len(parts) == 5 and parts[4] == "confirmations":
                return 201, alloc.confirm_line(actor_id=actor_id, application_id=application_id,
                                               line_id=line_id)
            if method == "POST" and len(parts) == 5 and parts[4] == "convert":
                return 200, alloc.convert_line(actor_id=actor_id, application_id=application_id,
                                               line_id=line_id)
            if method == "POST" and len(parts) == 5 and parts[4] == "abandon":
                return 200, alloc.abandon_line(actor_id=actor_id, application_id=application_id,
                                               line_id=line_id)
            if method == "POST" and len(parts) == 5 and parts[4] == "deliveries":
                return 201, alloc.record_delivery(actor_id=actor_id, application_id=application_id,
                                                  line_id=line_id, qty=body["qty"],
                                                  milestone_seq=body.get("milestone_seq"),
                                                  note=body.get("note", ""))
            if method == "POST" and len(parts) == 5 and parts[4] == "milestones":
                return 200, alloc.mark_milestone(actor_id=actor_id, application_id=application_id,
                                                 line_id=line_id,
                                                 milestone_seq=body["milestone_seq"],
                                                 met=bool(body["met"]))

    # /overrides/{id}/decision
    if method == "POST" and len(parts) == 3 and parts[0] == "overrides" and parts[2] == "decision":
        return 200, alloc.decide_override(actor_id=actor_id, override_id=parts[1],
                                          approve=bool(body["approve"]))

    # /providers/{id}/deliveries
    if method == "GET" and len(parts) == 3 and parts[0] == "providers" and parts[2] == "deliveries":
        return 200, alloc.provider_deliveries(parts[1])

    return 404, {"error": "route_not_found", "message": "接口不存在"}


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None, allocation: AllocationService | None = None
          ) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
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

        if parsed.path.strip("/").startswith("allocation"):
            alloc = allocation or getattr(service, "_allocation", None)
            if alloc is None:
                alloc = AllocationService(service.database, service.clock)
                service._allocation = alloc
            parts = [segment for segment in parsed.path.split("/") if segment][1:]
            query = parse_qs(parsed.query)
            return _allocation_route(alloc, method, parts, query, body, actor_id)
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    allocation: AllocationService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")},
                                self.allocation)
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
    Handler.service = DomainService(database)
    Handler.allocation = AllocationService(database, Handler.service.clock)
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
