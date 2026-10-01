"""品牌权益履约的 HTTP/JSON 边界。

路由可直接挂在标准库 ThreadingHTTPServer 上，也可通过 :func:`route`
在测试中离线调用。所有写接口都要求 ``X-Actor-Id``，读接口按角色裁剪。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from beverage_ops_foundation.errors import DomainError, ValidationError
from beverage_ops_foundation.storage import Database

from .errors import PlanConflictError
from .service import BrandRightsService


def _receipt(status_created: int, receipt) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else status_created), receipt.__dict__


def route(service: BrandRightsService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到品牌权益领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")

    def q(name: str, default: str | None = None) -> str | None:
        return query.get(name, [default])[0]

    try:
        # ---- 协议与版本 -------------------------------------------------
        if method == "POST" and parsed.path == "/brand/agreements":
            return _receipt(201, service.create_agreement(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/brand/agreement-versions":
            return _receipt(201, service.add_version(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/brand/agreements/sign":
            return _receipt(200, service.sign_agreement(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/brand/agreements/terminate":
            return _receipt(200, service.terminate_agreement(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/brand/agreements/renew":
            return _receipt(201, service.renew_agreement(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/brand/agreements":
            return 200, {"items": service.list_agreements(actor_id=actor_id)}
        if method == "GET" and parsed.path.startswith("/brand/agreements/"):
            agreement_id = parsed.path.rsplit("/", 1)[-1]
            return 200, service.get_agreement_detail(actor_id=actor_id, agreement_id=agreement_id)
        if method == "POST" and parsed.path == "/brand/conflicts/preview":
            findings = service.preview_conflicts(actor_id=actor_id, **body)
            return 200, {"conflict_count": len(findings), "findings": findings}

        # ---- 权益计划 ---------------------------------------------------
        if method == "POST" and parsed.path == "/brand/rights":
            return _receipt(201, service.add_right(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/brand/sessions":
            return _receipt(201, service.add_session(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/brand/sessions/status":
            return _receipt(200, service.update_session_status(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/brand/calendar":
            return 200, {"items": service.list_calendar(
                actor_id=actor_id, region=q("region"), venue=q("venue"))}
        if method == "POST" and parsed.path == "/brand/media-assets":
            return _receipt(201, service.add_media_asset(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/brand/media-assets/status":
            return _receipt(200, service.update_media_status(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/brand/deliverables":
            return _receipt(201, service.add_deliverable(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/brand/deliverables/delivery":
            return _receipt(200, service.register_delivery(actor_id=actor_id, **body))

        # ---- 费用承诺 ---------------------------------------------------
        if method == "POST" and parsed.path == "/brand/fees":
            return _receipt(201, service.add_fee(actor_id=actor_id, **body))

        # ---- 证据、验收、争议 -------------------------------------------
        if method == "POST" and parsed.path == "/brand/evidence":
            return _receipt(201, service.upload_evidence(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/brand/evidence":
            return 200, {"items": service.list_evidence(
                actor_id=actor_id, right_id=q("right_id"))}
        if method == "POST" and parsed.path == "/brand/acceptance":
            return _receipt(201, service.decide_acceptance(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/brand/disputes/resolve":
            return _receipt(200, service.resolve_dispute(actor_id=actor_id, **body))

        # ---- 付款与解释 -------------------------------------------------
        if method == "POST" and parsed.path == "/brand/payments":
            return _receipt(201, service.propose_payment(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/brand/payments/complete":
            return _receipt(200, service.complete_payment(actor_id=actor_id, **body))
        if method == "GET" and parsed.path.startswith("/brand/payments/"):
            payment_id = parsed.path.rsplit("/", 1)[-1]
            return 200, service.get_payment_explanation(actor_id=actor_id, payment_id=payment_id)

        return 404, {"error": "route_not_found", "message": "品牌权益接口不存在"}
    except PlanConflictError as exc:
        return exc.status, {"error": exc.code, "message": str(exc), "findings": exc.findings}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为品牌权益路由调用。"""

    service: BrandRightsService

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
    """启动品牌权益履约 HTTP 服务（与基础服务共用同一个 SQLite 库）。"""

    parser = argparse.ArgumentParser(description="启动品牌权益履约服务")
    parser.add_argument("--database", default="brand_rights.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = BrandRightsService(database)
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
