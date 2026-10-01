"""品牌权益履约的 HTTP/JSON 路由，按法务、市场、财务的职责边界授权。"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

from .brand_service import BrandConflictError, BrandService
from .errors import DomainError, ValidationError


def _int_query(query: dict[str, list[str]], key: str) -> int | None:
    raw = query.get(key, [None])[0]
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValidationError(f"{key} 必须是整数") from exc


def brand_route(service: BrandService, method: str, path: str, body: dict[str, Any] | None,
                headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]] | None:
    """处理 /brand 前缀的请求；无法识别时返回 None 交回基础路由。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    segments = [segment for segment in parsed.path.split("/") if segment]
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")
    if not segments or segments[0] != "brand":
        return None

    def call(action: str, **kwargs):
        receipt = getattr(service, action)(actor_id=actor_id, **kwargs)
        return 200 if receipt.replayed else 201, receipt.__dict__

    try:
        # POST /brand/agreements
        if method == "POST" and segments == ["brand", "agreements"]:
            return call("create_agreement", **body)
        # GET /brand/agreements
        if method == "GET" and segments == ["brand", "agreements"]:
            return 200, {"items": service.list_agreements(actor_id)}
        if len(segments) >= 3 and segments[1] == "agreements":
            agreement_id = segments[2]
            sub = segments[3] if len(segments) >= 4 else None
            # GET /brand/agreements/<id>
            if method == "GET" and len(segments) == 3:
                return 200, service.get_agreement_detail(actor_id, agreement_id)
            # POST /brand/agreements/<id>/revisions
            if method == "POST" and len(segments) == 4 and sub == "revisions":
                return call("revise_agreement", agreement_id=agreement_id, **body)
            # POST /brand/agreements/<id>/sign
            if method == "POST" and len(segments) == 4 and sub == "sign":
                return call("sign_version", agreement_id=agreement_id,
                            version_no=int(body.get("version_no", 0)),
                            signer_name=body.get("signer_name", ""),
                            request_id=body.get("request_id", ""))
            # GET /brand/agreements/<id>/conflicts
            if method == "GET" and len(segments) == 4 and sub == "conflicts":
                return 200, {"items": service.check_conflicts(
                    actor_id, agreement_id, _int_query(query, "version_no"))}
            # GET /brand/agreements/<id>/settlement
            if method == "GET" and len(segments) == 4 and sub == "settlement":
                return 200, service.settlement_report(actor_id, agreement_id)
            # GET /brand/agreements/<id>/entitlements/<eid>/tracking
            if method == "GET" and len(segments) == 6 and sub == "entitlements" \
                    and segments[5] == "tracking":
                return 200, service.get_entitlement_tracking(actor_id, agreement_id, segments[4])
            return 404, {"error": "route_not_found", "message": "品牌协议子接口不存在"}
        # 计划写入
        if method == "POST" and segments == ["brand", "sessions"]:
            return call("add_session", **body)
        if method == "POST" and segments == ["brand", "entitlements"]:
            return call("add_entitlement", **body)
        if method == "POST" and segments == ["brand", "fees"]:
            return call("add_fee", **body)
        if method == "POST" and segments == ["brand", "materials"]:
            return call("add_material", **body)
        if method == "POST" and segments == ["brand", "attachments"]:
            return call("upload_attachment", **body)
        # DELETE /brand/plan-items?agreement_id=..&kind=..&item_id=..&request_id=..
        if method == "DELETE" and segments == ["brand", "plan-items"]:
            return call("remove_plan_item",
                        agreement_id=query.get("agreement_id", [""])[0],
                        kind=query.get("kind", [""])[0],
                        item_id=query.get("item_id", [""])[0],
                        request_id=query.get("request_id", [""])[0])
        # 履约
        if method == "POST" and segments == ["brand", "material-deliveries"]:
            return call("mark_material_delivered", **body)
        if method == "POST" and segments == ["brand", "material-confirmations"]:
            return call("confirm_material", **body)
        if method == "POST" and segments == ["brand", "evidence"]:
            return call("upload_evidence", **body)
        if method == "POST" and segments == ["brand", "acceptances"]:
            return call("decide_acceptance", **body)
        if method == "POST" and segments == ["brand", "dispute-resolutions"]:
            return call("resolve_dispute", **body)
        if method == "POST" and segments == ["brand", "payments"]:
            return call("create_payment", **body)
        # GET /brand/payments/<id>
        if method == "GET" and len(segments) == 3 and segments[1] == "payments":
            return 200, service.explain_payment(actor_id, segments[2])
        return 404, {"error": "route_not_found", "message": "品牌接口不存在"}
    except BrandConflictError as exc:
        return exc.status, {"error": exc.code, "message": str(exc),
                            "conflicts": [conflict.to_dict() for conflict in exc.conflicts]}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}
