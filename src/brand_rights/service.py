"""品牌权益履约领域服务。

在基础服务的主体、操作者、站点、幂等收据与哈希审计链之上，提供：

- 协议与版本管理（draft/signed/superseded），续期是独立新协议，不复制旧场次；
- 权益计划（露出位置、排他品类、供货数量、活动窗口、媒体资产、物料）；
- 签署或变更前的时间窗与排他范围冲突识别；
- 现场证据验收（通过/部分验收/退回补交/争议），争议只冻结相关费用；
- 费用承诺与付款的逐笔解释，重复凭证不会产生第二次验收。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from beverage_ops_foundation.audit import append_event, canonical_json, digest
from beverage_ops_foundation.clock import Clock, SystemClock
from beverage_ops_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from beverage_ops_foundation.models import Actor, WriteReceipt
from beverage_ops_foundation.service import IDENTIFIER
from beverage_ops_foundation.storage import Database

from . import permissions
from .conflicts import detect_conflicts
from .errors import PlanConflictError
from .schema import BRAND_RIGHTS_SCHEMA

EVENT_TYPES = {"racing", "football", "music_festival", "other"}
RIGHT_KINDS = {"exposure", "exclusive_category", "supply", "activity_window",
               "media_asset", "material", "other"}
SCOPE_TYPES = {"global", "region", "venue", "event_type", "event"}
DECISIONS = {"accepted", "partial", "returned", "disputed"}


class BrandRightsService:
    """协调品牌权益的权限、事务、冲突检测、验收与结算规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        database.connection.executescript(BRAND_RIGHTS_SCHEMA)

    # ------------------------------------------------------------------
    # 通用辅助
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    @staticmethod
    def _id(value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    @staticmethod
    def _text(value: str, field: str, limit: int = 300) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _window(self, start: str | None, end: str | None) -> tuple[str | None, str | None]:
        if start and end and start >= end:
            raise ValidationError("时间窗开始必须早于结束")
        return (start or None), (end or None)

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _can(self, actor: Actor, action: str) -> None:
        if not permissions.can_write(actor.role, action):
            raise PermissionDenied(f"角色 {actor.role} 不能执行 {action}")

    def _can_read(self, actor: Actor, family: str) -> None:
        if not permissions.can_read(actor.role, family):
            raise PermissionDenied(f"角色 {actor.role} 不能查看 {family} 明细")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action,
                     resource_type=resource_type, resource_id=resource_id,
                     detail=detail, occurred_at=self._now())

    def _agreement_row(self, connection, agreement_id: str):
        row = connection.execute(
            "SELECT * FROM br_agreements WHERE agreement_id=?", (agreement_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("协议不存在")
        return row

    def _scope_access(self, actor: Actor, organization_id: str) -> None:
        if actor.role != "admin" and actor.organization_id != organization_id:
            raise PermissionDenied("不能访问其他经营主体的协议")

    def _draft_version(self, connection, agreement_id: str, version_no: int):
        row = connection.execute(
            "SELECT * FROM br_agreement_versions WHERE agreement_id=? AND version_no=?",
            (agreement_id, version_no),
        ).fetchone()
        if row is None:
            raise NotFoundError("协议版本不存在")
        if row["status"] != "draft":
            raise ConflictError("只有 draft 版本可以修改")
        return row

    def _right_row(self, connection, right_id: str):
        row = connection.execute("SELECT * FROM br_rights WHERE right_id=?", (right_id,)).fetchone()
        if row is None:
            raise NotFoundError("权益不存在")
        return row

    # ------------------------------------------------------------------
    # 协议与版本
    # ------------------------------------------------------------------

    def create_agreement(self, *, request_id: str, actor_id: str, agreement_id: str,
                         partner_id: str, partner_name: str,
                         organization_id: str | None = None,
                         renewal_of_agreement_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id,
                   "partner_id": partner_id, "partner_name": partner_name,
                   "organization_id": organization_id, "renewal_of": renewal_of_agreement_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._can(actor, "agreement.write")
            agreement_id = self._id(agreement_id, "agreement_id")
            partner_id = self._id(partner_id, "partner_id")
            partner_name = self._text(partner_name, "partner_name")
            org_id = organization_id or actor.organization_id
            if actor.role != "admin" and org_id != actor.organization_id:
                raise PermissionDenied("不能为其他经营主体建档")
            if renewal_of_agreement_id is not None:
                source = self._agreement_row(conn, renewal_of_agreement_id)
                self._scope_access(actor, source["organization_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO br_agreements(agreement_id,organization_id,partner_id,partner_name,"
                        "status,current_version_no,renewal_of_agreement_id,created_by,created_at) "
                        "VALUES(?,?,?,?, 'draft', 0, ?, ?, ?)",
                        (agreement_id, org_id, partner_id, partner_name,
                         renewal_of_agreement_id, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("协议编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="brand.agreement.created",
                            resource_type="br_agreement", resource_id=agreement_id,
                            detail={"partner_id": partner_id, "renewal_of": renewal_of_agreement_id})
                return "br_agreement", agreement_id, {"agreement_id": agreement_id}

            return self._idempotent(conn, request_id=request_id, action="brand.create_agreement",
                                    payload=payload, create=create)

    def add_version(self, *, request_id: str, actor_id: str, agreement_id: str,
                    document_ref: str, content: dict[str, Any],
                    change_note: str = "") -> WriteReceipt:
        if not isinstance(content, dict) or not content:
            raise ValidationError("content 必须是非空对象")
        payload = {"actor_id": actor_id, "agreement_id": agreement_id,
                   "document_ref": document_ref, "content": content, "change_note": change_note}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._can(actor, "agreement.write")
            agreement = self._agreement_row(conn, agreement_id)
            self._scope_access(actor, agreement["organization_id"])
            if agreement["status"] == "terminated":
                raise ConflictError("已终止协议不能新增版本")
            document_ref = self._text(document_ref, "document_ref")
            change_note = str(change_note or "")[:500]
            content_hash = digest(content)

            def create() -> tuple[str, str, dict[str, Any]]:
                next_no = conn.execute(
                    "SELECT COALESCE(MAX(version_no), 0) + 1 AS n "
                    "FROM br_agreement_versions WHERE agreement_id=?",
                    (agreement_id,),
                ).fetchone()["n"]
                conn.execute(
                    "INSERT INTO br_agreement_versions(agreement_id,version_no,status,document_ref,"
                    "content_hash,change_note,created_by,created_at) VALUES(?,?,'draft',?,?,?,?,?)",
                    (agreement_id, next_no, document_ref, content_hash, change_note,
                     actor_id, self._now()),
                )
                self._audit(conn, actor_id=actor_id, action="brand.version.added",
                            resource_type="br_agreement_version",
                            resource_id=f"{agreement_id}:v{next_no}",
                            detail={"version_no": next_no, "document_ref": document_ref,
                                    "content_hash": content_hash})
                return "br_agreement_version", f"{agreement_id}:v{next_no}", {
                    "agreement_id": agreement_id, "version_no": next_no}

            return self._idempotent(conn, request_id=request_id, action="brand.add_version",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 权益计划
    # ------------------------------------------------------------------

    def add_right(self, *, request_id: str, actor_id: str, agreement_id: str, version_no: int,
                  event_type: str, right_kind: str, title: str,
                  region: str = "", venue: str = "",
                  window_start: str | None = None, window_end: str | None = None,
                  spec: dict[str, Any] | None = None,
                  exclusivity: dict[str, Any] | None = None) -> WriteReceipt:
        spec = spec or {}
        if not isinstance(spec, dict):
            raise ValidationError("spec 必须是对象")
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "version_no": version_no,
                   "event_type": event_type, "right_kind": right_kind, "title": title,
                   "region": region, "venue": venue, "window_start": window_start,
                   "window_end": window_end, "spec": spec, "exclusivity": exclusivity}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._can(actor, "right.write")
            agreement = self._agreement_row(conn, agreement_id)
            self._scope_access(actor, agreement["organization_id"])
            self._draft_version(conn, agreement_id, int(version_no))
            if event_type not in EVENT_TYPES:
                raise ValidationError("event_type 不支持")
            if right_kind not in RIGHT_KINDS:
                raise ValidationError("right_kind 不支持")
            title = self._text(title, "title")
            window_start, window_end = self._window(window_start, window_end)
            ex_row = None
            if exclusivity is not None:
                if right_kind != "exclusive_category":
                    raise ValidationError("只有 exclusive_category 权益可以声明排他范围")
                category = self._text(exclusivity.get("category_label", ""), "category_label")
                scope_type = exclusivity.get("scope_type", "")
                if scope_type not in SCOPE_TYPES:
                    raise ValidationError("排他 scope_type 不支持")
                scope_value = str(exclusivity.get("scope_value", "")).strip()
                if scope_type != "global" and not scope_value:
                    raise ValidationError("非全局排他必须给出 scope_value")
                ex_start, ex_end = self._window(exclusivity.get("window_start") or window_start,
                                                exclusivity.get("window_end") or window_end)
                ex_row = {"category_label": category, "scope_type": scope_type,
                          "scope_value": scope_value, "window_start": ex_start, "window_end": ex_end}

            def create() -> tuple[str, str, dict[str, Any]]:
                right_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO br_rights(right_id,agreement_id,version_no,event_type,right_kind,"
                    "title,region,venue,window_start,window_end,spec_json,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?, 'planned', ?)",
                    (right_id, agreement_id, version_no, event_type, right_kind, title,
                     str(region or "").strip(), str(venue or "").strip(),
                     window_start, window_end, canonical_json(spec), self._now()),
                )
                if ex_row:
                    conn.execute(
                        "INSERT INTO br_exclusivity(right_id,category_label,scope_type,scope_value,"
                        "window_start,window_end) VALUES(?,?,?,?,?,?)",
                        (right_id, ex_row["category_label"], ex_row["scope_type"],
                         ex_row["scope_value"], ex_row["window_start"], ex_row["window_end"]),
                    )
                self._audit(conn, actor_id=actor_id, action="brand.right.added",
                            resource_type="br_right", resource_id=right_id,
                            detail={"agreement_id": agreement_id, "version_no": version_no,
                                    "right_kind": right_kind, "title": title,
                                    "exclusivity": ex_row})
                return "br_right", right_id, {"right_id": right_id}

            return self._idempotent(conn, request_id=request_id, action="brand.add_right",
                                    payload=payload, create=create)

    def add_session(self, *, request_id: str, actor_id: str, source_right_id: str, name: str,
                    venue: str, starts_at: str, ends_at: str, region: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "source_right_id": source_right_id, "name": name,
                   "venue": venue, "starts_at": starts_at, "ends_at": ends_at, "region": region}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._can(actor, "session.write")
            right = self._right_row(conn, source_right_id)
            agreement = self._agreement_row(conn, right["agreement_id"])
            self._scope_access(actor, agreement["organization_id"])
            if agreement["status"] != "signed" or right["version_no"] != agreement["current_version_no"]:
                raise ConflictError("场次只能安排在已签署协议当前版本的权益上")
            name = self._text(name, "name")
            venue = self._text(venue, "venue", 120)
            starts_at, ends_at = self._window(starts_at, ends_at)
            if not starts_at:
                raise ValidationError("场次必须给出开始时间")

            def create() -> tuple[str, str, dict[str, Any]]:
                session_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO br_sessions(session_id,source_right_id,agreement_id,name,region,"
                    "venue,starts_at,ends_at,status,created_at) VALUES(?,?,?,?,?,?,?,?,'scheduled',?)",
                    (session_id, source_right_id, right["agreement_id"], name,
                     str(region or "").strip(), venue, starts_at, ends_at, self._now()),
                )
                self._audit(conn, actor_id=actor_id, action="brand.session.added",
                            resource_type="br_session", resource_id=session_id,
                            detail={"source_right_id": source_right_id, "venue": venue,
                                    "starts_at": starts_at, "ends_at": ends_at})
                return "br_session", session_id, {"session_id": session_id}

            return self._idempotent(conn, request_id=request_id, action="brand.add_session",
                                    payload=payload, create=create)

    def update_session_status(self, *, request_id: str, actor_id: str,
                              session_id: str, status: str) -> WriteReceipt:
        if status not in {"scheduled", "live", "completed", "cancelled"}:
            raise ValidationError("场次状态不支持")
        payload = {"actor_id": actor_id, "session_id": session_id, "status": status}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._can(actor, "session.write")
            row = conn.execute("SELECT * FROM br_sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                raise NotFoundError("场次不存在")
            agreement = self._agreement_row(conn, row["agreement_id"])
            self._scope_access(actor, agreement["organization_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute("UPDATE br_sessions SET status=? WHERE session_id=?", (status, session_id))
                # 每场次的 per_session 费用只在首次完成时计入应付一次
                if status == "completed" and row["status"] != "completed":
                    fees = conn.execute(
                        "SELECT * FROM br_fee_items WHERE right_id=? AND due_kind='per_session' "
                        "AND status!='void'", (row["source_right_id"],)
                    ).fetchall()
                    for fee in fees:
                        self._increase_payable(conn, fee, fee["amount_minor"])
                self._audit(conn, actor_id=actor_id, action="brand.session.updated",
                            resource_type="br_session", resource_id=session_id,
                            detail={"status": status})
                return "br_session", session_id, {"session_id": session_id, "status": status}

            return self._idempotent(conn, request_id=request_id,
                                    action="brand.update_session", payload=payload, create=create)

    def add_media_asset(self, *, request_id: str, actor_id: str, right_id: str,
                        placement: str, spec: str = "",
                        delivery_deadline: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "right_id": right_id, "placement": placement,
                   "spec": spec, "delivery_deadline": delivery_deadline}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._can(actor, "asset.write")
            right = self._right_row(conn, right_id)
            agreement = self._agreement_row(conn, right["agreement_id"])
            self._scope_access(actor, agreement["organization_id"])
            placement = self._text(placement, "placement", 200)

            def create() -> tuple[str, str, dict[str, Any]]:
                asset_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO br_media_assets(asset_id,right_id,placement,spec,delivery_deadline,"
                    "status,created_at) VALUES(?,?,?,?,?, 'planned', ?)",
                    (asset_id, right_id, placement, str(spec or "")[:1000],
                     delivery_deadline, self._now()),
                )
                self._audit(conn, actor_id=actor_id, action="brand.media.added",
                            resource_type="br_media_asset", resource_id=asset_id,
                            detail={"right_id": right_id, "placement": placement})
                return "br_media_asset", asset_id, {"asset_id": asset_id}

            return self._idempotent(conn, request_id=request_id, action="brand.add_media",
                                    payload=payload, create=create)

    def update_media_status(self, *, request_id: str, actor_id: str,
                            asset_id: str, status: str) -> WriteReceipt:
        if status not in {"planned", "in_production", "delivered", "rejected"}:
            raise ValidationError("媒体资产状态不支持")
        payload = {"actor_id": actor_id, "asset_id": asset_id, "status": status}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._can(actor, "asset.write")
            row = conn.execute("SELECT * FROM br_media_assets WHERE asset_id=?", (asset_id,)).fetchone()
            if row is None:
                raise NotFoundError("媒体资产不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute("UPDATE br_media_assets SET status=? WHERE asset_id=?", (status, asset_id))
                self._audit(conn, actor_id=actor_id, action="brand.media.updated",
                            resource_type="br_media_asset", resource_id=asset_id,
                            detail={"status": status})
                return "br_media_asset", asset_id, {"asset_id": asset_id, "status": status}

            return self._idempotent(conn, request_id=request_id, action="brand.update_media",
                                    payload=payload, create=create)

    def add_deliverable(self, *, request_id: str, actor_id: str, right_id: str,
                        material_type: str, quantity_due: int,
                        due_at: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "right_id": right_id, "material_type": material_type,
                   "quantity_due": quantity_due, "due_at": due_at}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._can(actor, "deliverable.write")
            right = self._right_row(conn, right_id)
            agreement = self._agreement_row(conn, right["agreement_id"])
            self._scope_access(actor, agreement["organization_id"])
            material_type = self._text(material_type, "material_type", 120)
            if not isinstance(quantity_due, int) or quantity_due < 0:
                raise ValidationError("quantity_due 必须是非负整数")

            def create() -> tuple[str, str, dict[str, Any]]:
                deliverable_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO br_deliverables(deliverable_id,right_id,material_type,quantity_due,"
                    "quantity_delivered,due_at,status,created_at) VALUES(?,?,?,? ,0,?, 'pending', ?)",
                    (deliverable_id, right_id, material_type, quantity_due, due_at, self._now()),
                )
                self._audit(conn, actor_id=actor_id, action="brand.deliverable.added",
                            resource_type="br_deliverable", resource_id=deliverable_id,
                            detail={"right_id": right_id, "quantity_due": quantity_due})
                return "br_deliverable", deliverable_id, {"deliverable_id": deliverable_id}

            return self._idempotent(conn, request_id=request_id, action="brand.add_deliverable",
                                    payload=payload, create=create)

    def register_delivery(self, *, request_id: str, actor_id: str,
                          deliverable_id: str, quantity: int) -> WriteReceipt:
        payload = {"actor_id": actor_id, "deliverable_id": deliverable_id, "quantity": quantity}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._can(actor, "deliverable.write")
            row = conn.execute(
                "SELECT * FROM br_deliverables WHERE deliverable_id=?", (deliverable_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("物料不存在")
            right = self._right_row(conn, row["right_id"])
            agreement = self._agreement_row(conn, right["agreement_id"])
            self._scope_access(actor, agreement["organization_id"])
            if agreement["status"] != "signed" or right["version_no"] != agreement["current_version_no"]:
                raise ConflictError("只能就已签署协议当前版本的物料登记交付")
            if not isinstance(quantity, int) or quantity <= 0:
                raise ValidationError("本次交付数量必须是正整数")
            new_total = row["quantity_delivered"] + quantity
            if new_total > row["quantity_due"]:
                raise ConflictError("累计交付超过约定数量")

            def create() -> tuple[str, str, dict[str, Any]]:
                status = "delivered" if new_total == row["quantity_due"] else "partial"
                conn.execute(
                    "UPDATE br_deliverables SET quantity_delivered=?, status=? WHERE deliverable_id=?",
                    (new_total, status, deliverable_id),
                )
                self._audit(conn, actor_id=actor_id, action="brand.deliverable.delivered",
                            resource_type="br_deliverable", resource_id=deliverable_id,
                            detail={"quantity": quantity, "total": new_total, "status": status})
                return "br_deliverable", deliverable_id, {
                    "deliverable_id": deliverable_id, "quantity_delivered": new_total, "status": status}

            return self._idempotent(conn, request_id=request_id, action="brand.register_delivery",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 费用承诺
    # ------------------------------------------------------------------

    def add_fee(self, *, request_id: str, actor_id: str, right_id: str, label: str,
                amount_minor: int, currency: str, due_kind: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "right_id": right_id, "label": label,
                   "amount_minor": amount_minor, "currency": currency, "due_kind": due_kind}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._can(actor, "fee.write")
            right = self._right_row(conn, right_id)
            agreement = self._agreement_row(conn, right["agreement_id"])
            self._scope_access(actor, agreement["organization_id"])
            label = self._text(label, "label", 200)
            currency = self._text(currency, "currency", 3)
            if due_kind not in {"fixed", "per_session", "per_acceptance"}:
                raise ValidationError("due_kind 不支持")
            if not isinstance(amount_minor, int) or amount_minor < 0:
                raise ValidationError("amount_minor 必须是非负整数")

            def create() -> tuple[str, str, dict[str, Any]]:
                fee_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO br_fee_items(fee_id,right_id,agreement_id,label,amount_minor,currency,"
                    "due_kind,status,created_at) VALUES(?,?,?,?,?,?,?, 'planned', ?)",
                    (fee_id, right_id, right["agreement_id"], label, amount_minor, currency,
                     due_kind, self._now()),
                )
                self._audit(conn, actor_id=actor_id, action="brand.fee.added",
                            resource_type="br_fee", resource_id=fee_id,
                            detail={"right_id": right_id, "amount_minor": amount_minor,
                                    "currency": currency, "due_kind": due_kind})
                return "br_fee", fee_id, {"fee_id": fee_id}

            return self._idempotent(conn, request_id=request_id, action="brand.add_fee",
                                    payload=payload, create=create)

    @staticmethod
    def _increase_payable(conn, fee_row, amount_minor: int) -> None:
        """把已确认的金额计入应付；冻结中的费用只累加基数，不改状态。"""

        new_payable = fee_row["payable_minor"] + amount_minor
        if fee_row["status"] == "frozen":
            conn.execute("UPDATE br_fee_items SET payable_minor=? WHERE fee_id=?",
                         (new_payable, fee_row["fee_id"]))
        else:
            conn.execute("UPDATE br_fee_items SET payable_minor=?, status='payable' WHERE fee_id=?",
                         (new_payable, fee_row["fee_id"]))

    # ------------------------------------------------------------------
    # 签署 / 续期 / 终止与冲突识别
    # ------------------------------------------------------------------

    def _load_rights_snapshot(self, conn) -> dict[str, dict[str, Any]]:
        """读取所有权益及其排他范围，键为 right_id。"""

        snapshot: dict[str, dict[str, Any]] = {}
        for row in conn.execute("SELECT * FROM br_rights WHERE status!='cancelled'"):
            snapshot[row["right_id"]] = {
                "right_id": row["right_id"], "agreement_id": row["agreement_id"],
                "version_no": row["version_no"], "event_type": row["event_type"],
                "right_kind": row["right_kind"], "title": row["title"],
                "region": row["region"], "venue": row["venue"],
                "window_start": row["window_start"], "window_end": row["window_end"],
                "spec": json.loads(row["spec_json"]), "exclusivity": None,
            }
        for row in conn.execute("SELECT * FROM br_exclusivity"):
            if row["right_id"] in snapshot:
                snapshot[row["right_id"]]["exclusivity"] = {
                    "category_label": row["category_label"], "scope_type": row["scope_type"],
                    "scope_value": row["scope_value"], "window_start": row["window_start"],
                    "window_end": row["window_end"],
                }
        return snapshot

    def preview_conflicts(self, *, actor_id: str, agreement_id: str, version_no: int) -> list[dict[str, Any]]:
        """签署前预检：候选版本相对其他协议现行版本的冲突清单。"""

        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._can_read(actor, "right")
            agreement = self._agreement_row(conn, agreement_id)
            self._scope_access(actor, agreement["organization_id"])
            self._draft_version(conn, agreement_id, int(version_no))
            return self._conflicts_for_version(conn, agreement_id, int(version_no))

    def _conflicts_for_version(self, conn, agreement_id: str, version_no: int) -> list[dict[str, Any]]:
        snapshot = self._load_rights_snapshot(conn)
        candidate = [r for r in snapshot.values()
                     if r["agreement_id"] == agreement_id and r["version_no"] == version_no]
        # 现行版本：已签署协议当前版本上的权益（候选自身协议除外）
        current_versions = {
            row["agreement_id"]: row["current_version_no"]
            for row in conn.execute("SELECT * FROM br_agreements WHERE status='signed'")
            if row["agreement_id"] != agreement_id
        }
        committed = [r for r in snapshot.values()
                     if current_versions.get(r["agreement_id"]) == r["version_no"]]
        return detect_conflicts(candidate, committed)

    def sign_agreement(self, *, request_id: str, actor_id: str,
                       agreement_id: str, version_no: int | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "version_no": version_no}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._can(actor, "agreement.sign")
            agreement = self._agreement_row(conn, agreement_id)
            self._scope_access(actor, agreement["organization_id"])
            target_no = version_no if version_no is not None else (
                conn.execute("SELECT COALESCE(MAX(version_no), 0) AS n "
                             "FROM br_agreement_versions WHERE agreement_id=?",
                             (agreement_id,)).fetchone()["n"])
            version = self._draft_version(conn, agreement_id, target_no)
            rights_count = conn.execute(
                "SELECT COUNT(*) AS c FROM br_rights WHERE agreement_id=? AND version_no=?",
                (agreement_id, target_no),
            ).fetchone()["c"]
            if rights_count == 0:
                raise ValidationError("版本没有任何权益计划，不能签署")
            findings = self._conflicts_for_version(conn, agreement_id, target_no)
            if findings:
                raise PlanConflictError(findings)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                # 旧签署版本让位
                conn.execute(
                    "UPDATE br_agreement_versions SET status='superseded' "
                    "WHERE agreement_id=? AND status='signed'",
                    (agreement_id,),
                )
                conn.execute(
                    "UPDATE br_agreement_versions SET status='signed', signed_by=?, signed_at=? "
                    "WHERE agreement_id=? AND version_no=?",
                    (actor_id, now, agreement_id, target_no),
                )
                conn.execute(
                    "UPDATE br_agreements SET status='signed', current_version_no=?, signed_at=? "
                    "WHERE agreement_id=?",
                    (target_no, now, agreement_id),
                )
                # 固定费用在签署后成为应付
                fees = conn.execute(
                    "SELECT * FROM br_fee_items WHERE agreement_id=? AND due_kind='fixed' "
                    "AND status='planned'", (agreement_id,)
                ).fetchall()
                for fee in fees:
                    conn.execute(
                        "UPDATE br_fee_items SET status='payable', payable_minor=amount_minor "
                        "WHERE fee_id=?", (fee["fee_id"],)
                    )
                self._audit(conn, actor_id=actor_id, action="brand.agreement.signed",
                            resource_type="br_agreement", resource_id=agreement_id,
                            detail={"version_no": target_no, "rights_count": rights_count})
                return "br_agreement", agreement_id, {
                    "agreement_id": agreement_id, "version_no": target_no}

            return self._idempotent(conn, request_id=request_id, action="brand.sign_agreement",
                                    payload=payload, create=create)

    def terminate_agreement(self, *, request_id: str, actor_id: str,
                            agreement_id: str, reason: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._can(actor, "agreement.terminate")
            agreement = self._agreement_row(conn, agreement_id)
            self._scope_access(actor, agreement["organization_id"])
            if agreement["status"] == "terminated":
                raise ConflictError("协议已经终止")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                conn.execute(
                    "UPDATE br_agreements SET status='terminated', terminated_at=? WHERE agreement_id=?",
                    (now, agreement_id),
                )
                conn.execute(
                    "UPDATE br_rights SET status='cancelled' WHERE agreement_id=? "
                    "AND version_no=? AND status IN ('planned','active')",
                    (agreement_id, agreement["current_version_no"]),
                )
                self._audit(conn, actor_id=actor_id, action="brand.agreement.terminated",
                            resource_type="br_agreement", resource_id=agreement_id,
                            detail={"reason": str(reason or "")[:500]})
                return "br_agreement", agreement_id, {"agreement_id": agreement_id}

            return self._idempotent(conn, request_id=request_id,
                                    action="brand.terminate_agreement", payload=payload,
                                    create=create)

    def renew_agreement(self, *, request_id: str, actor_id: str, new_agreement_id: str,
                        source_agreement_id: str, document_ref: str,
                        content: dict[str, Any]) -> WriteReceipt:
        """以旧协议为底稿续期：复制权益文案到新协议草稿，但场次/物料/媒体/费用一律不延续。"""

        if not isinstance(content, dict) or not content:
            raise ValidationError("content 必须是非空对象")
        payload = {"actor_id": actor_id, "new_agreement_id": new_agreement_id,
                   "source_agreement_id": source_agreement_id,
                   "document_ref": document_ref, "content": content}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._can(actor, "agreement.write")
            source = self._agreement_row(conn, source_agreement_id)
            self._scope_access(actor, source["organization_id"])
            if source["current_version_no"] == 0:
                raise ConflictError("未签署的协议不存在可续期版本")
            document_ref = self._text(document_ref, "document_ref")
            new_agreement_id = self._id(new_agreement_id, "new_agreement_id")
            content_hash = digest(content)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                try:
                    conn.execute(
                        "INSERT INTO br_agreements(agreement_id,organization_id,partner_id,partner_name,"
                        "status,current_version_no,renewal_of_agreement_id,created_by,created_at) "
                        "VALUES(?,?,?,?, 'draft', 0, ?, ?, ?)",
                        (new_agreement_id, source["organization_id"], source["partner_id"],
                         source["partner_name"], source_agreement_id, actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("新协议编号已经存在") from exc
                conn.execute(
                    "INSERT INTO br_agreement_versions(agreement_id,version_no,status,document_ref,"
                    "content_hash,change_note,created_by,created_at) VALUES(?, 1, 'draft', ?, ?, ?, ?, ?)",
                    (new_agreement_id, document_ref, content_hash,
                     f"续期自 {source_agreement_id}:v{source['current_version_no']}", actor_id, now),
                )
                old_rows = conn.execute(
                    "SELECT * FROM br_rights WHERE agreement_id=? AND version_no=? AND status!='cancelled'",
                    (source_agreement_id, source["current_version_no"]),
                ).fetchall()
                copied = []
                for old in old_rows:
                    right_id = uuid.uuid4().hex
                    conn.execute(
                        "INSERT INTO br_rights(right_id,agreement_id,version_no,event_type,right_kind,"
                        "title,region,venue,window_start,window_end,spec_json,status,"
                        "copied_from_right_id,created_at) VALUES(?,? ,1, ?,?,?,?,?,?,?,?, 'planned', ?, ?)",
                        (right_id, new_agreement_id, old["event_type"], old["right_kind"],
                         old["title"], old["region"], old["venue"], old["window_start"],
                         old["window_end"], old["spec_json"], old["right_id"], now),
                    )
                    ex = conn.execute("SELECT * FROM br_exclusivity WHERE right_id=?",
                                      (old["right_id"],)).fetchone()
                    if ex:
                        conn.execute(
                            "INSERT INTO br_exclusivity(right_id,category_label,scope_type,scope_value,"
                            "window_start,window_end) VALUES(?,?,?,?,?,?)",
                            (right_id, ex["category_label"], ex["scope_type"], ex["scope_value"],
                             ex["window_start"], ex["window_end"]),
                        )
                    copied.append(right_id)
                self._audit(conn, actor_id=actor_id, action="brand.agreement.renewed",
                            resource_type="br_agreement", resource_id=new_agreement_id,
                            detail={"source_agreement_id": source_agreement_id,
                                    "copied_rights": copied,
                                    "copied_sessions": 0,
                                    "note": "场次、物料、媒体资产与费用不随续期延续"})
                return "br_agreement", new_agreement_id, {
                    "agreement_id": new_agreement_id, "version_no": 1, "copied_right_count": len(copied)}

            return self._idempotent(conn, request_id=request_id, action="brand.renew_agreement",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 证据上传与验收
    # ------------------------------------------------------------------

    def upload_evidence(self, *, request_id: str, actor_id: str, right_id: str,
                        evidence_kind: str, file_ref: str, content_hash: str,
                        session_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "right_id": right_id, "evidence_kind": evidence_kind,
                   "file_ref": file_ref, "content_hash": content_hash, "session_id": session_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._can(actor, "evidence.upload")
            right = self._right_row(conn, right_id)
            agreement = self._agreement_row(conn, right["agreement_id"])
            self._scope_access(actor, agreement["organization_id"])
            if agreement["status"] != "signed" or right["version_no"] != agreement["current_version_no"]:
                raise ConflictError("只能对已签署协议当前版本的权益上传现场凭证")
            if right["status"] == "cancelled":
                raise ConflictError("权益已取消，不能上传凭证")
            evidence_kind = self._text(evidence_kind, "evidence_kind", 80)
            file_ref = self._text(file_ref, "file_ref", 300)
            content_hash = self._text(content_hash, "content_hash", 128)
            if session_id is not None:
                session = conn.execute(
                    "SELECT 1 FROM br_sessions WHERE session_id=? AND source_right_id=?",
                    (session_id, right_id),
                ).fetchone()
                if session is None:
                    raise ValidationError("凭证关联的场次不属于该权益")

            def create() -> tuple[str, str, dict[str, Any]]:
                # 全局内容哈希去重：同一份凭证不能制造第二次验收，也不能佐证别的权益
                duplicate = conn.execute(
                    "SELECT * FROM br_evidence WHERE content_hash=?", (content_hash,)
                ).fetchone()
                if duplicate is not None:
                    if duplicate["right_id"] != right_id:
                        raise ConflictError("同一凭证已经用于其他权益，不能重复采信")
                    self._audit(conn, actor_id=actor_id, action="brand.evidence.deduplicated",
                                resource_type="br_evidence", resource_id=duplicate["evidence_id"],
                                detail={"right_id": right_id, "content_hash": content_hash})
                    return "br_evidence", duplicate["evidence_id"], {
                        "evidence_id": duplicate["evidence_id"], "deduplicated": True,
                        "latest_decision": duplicate["latest_decision"]}
                evidence_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO br_evidence(evidence_id,right_id,session_id,evidence_kind,file_ref,"
                    "content_hash,uploaded_by,uploaded_at,latest_decision) "
                    "VALUES(?,?,?,?,?,?,?,?,'pending')",
                    (evidence_id, right_id, session_id, evidence_kind, file_ref,
                     content_hash, actor_id, self._now()),
                )
                self._audit(conn, actor_id=actor_id, action="brand.evidence.uploaded",
                            resource_type="br_evidence", resource_id=evidence_id,
                            detail={"right_id": right_id, "content_hash": content_hash})
                return "br_evidence", evidence_id, {"evidence_id": evidence_id, "deduplicated": False}

            return self._idempotent(conn, request_id=request_id, action="brand.upload_evidence",
                                    payload=payload, create=create)

    def decide_acceptance(self, *, request_id: str, actor_id: str, evidence_id: str,
                          decision: str, accepted_ratio: int | None = None,
                          note: str = "", dispute_reason: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "evidence_id": evidence_id, "decision": decision,
                   "accepted_ratio": accepted_ratio, "note": note, "dispute_reason": dispute_reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._can(actor, "acceptance.decide")
            evidence = conn.execute(
                "SELECT * FROM br_evidence WHERE evidence_id=?", (evidence_id,)
            ).fetchone()
            if evidence is None:
                raise NotFoundError("凭证不存在")
            right = self._right_row(conn, evidence["right_id"])
            agreement = self._agreement_row(conn, right["agreement_id"])
            self._scope_access(actor, agreement["organization_id"])
            if decision not in DECISIONS:
                raise ValidationError("decision 不支持")
            ratio = self._normalize_ratio(decision, accepted_ratio)
            note = str(note or "")[:1000]

            def create() -> tuple[str, str, dict[str, Any]]:
                acceptance_id = uuid.uuid4().hex
                dispute_id = None
                if decision == "disputed":
                    dispute_id = self._open_dispute(
                        conn, actor=actor, right=right, evidence=evidence,
                        reason=dispute_reason or note or "现场验收争议",
                        accepted_ratio=ratio, note=note)
                conn.execute(
                    "INSERT INTO br_acceptance_events(acceptance_id,evidence_id,right_id,decision,"
                    "accepted_ratio,note,decided_by,decided_at,supersedes_acceptance_id,dispute_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (acceptance_id, evidence_id, evidence["right_id"], decision, ratio, note,
                     actor_id, self._now(), evidence["latest_acceptance_id"], dispute_id),
                )
                conn.execute(
                    "UPDATE br_evidence SET latest_decision=?, latest_ratio=?, "
                    "latest_acceptance_id=? WHERE evidence_id=?",
                    (decision, ratio, acceptance_id, evidence_id),
                )
                if decision in ("accepted", "partial"):
                    new_right_status = "fulfilled" if ratio == 100 else "partial"
                    conn.execute("UPDATE br_rights SET status=? WHERE right_id=? AND status!='cancelled'",
                                 (new_right_status, evidence["right_id"]))
                elif decision == "disputed":
                    conn.execute("UPDATE br_rights SET status='disputed' WHERE right_id=?",
                                 (evidence["right_id"],))
                # 所有裁决都按最新有效决策重算应付，防止退回后仍挂着旧应付
                self._recompute_right_acceptance_fees(conn, evidence["right_id"])
                self._audit(conn, actor_id=actor_id, action="brand.acceptance.decided",
                            resource_type="br_acceptance", resource_id=acceptance_id,
                            detail={"evidence_id": evidence_id, "decision": decision,
                                    "accepted_ratio": ratio, "dispute_id": dispute_id})
                return "br_acceptance", acceptance_id, {
                    "acceptance_id": acceptance_id, "decision": decision,
                    "accepted_ratio": ratio, "dispute_id": dispute_id}

            return self._idempotent(conn, request_id=request_id,
                                    action="brand.decide_acceptance", payload=payload,
                                    create=create)

    @staticmethod
    def _normalize_ratio(decision: str, ratio: int | None) -> int:
        if decision == "accepted":
            return 100
        if decision == "returned":
            return 0
        if decision == "disputed":
            if ratio is None:
                return 0
        if not isinstance(ratio, int) or not (0 <= ratio <= 100):
            raise ValidationError("accepted_ratio 必须在 0 到 100 之间")
        if decision == "partial" and not (1 <= ratio <= 99):
            raise ValidationError("部分验收比例必须在 1 到 99 之间")
        return ratio

    @staticmethod
    def _recompute_acceptance_payable(conn, fee_row) -> None:
        """按该权益下各凭证当前有效裁决重算 per_acceptance 应付，避免重复计付。

        同一凭证无论经历多少次裁决（部分→通过→争议→裁决），只按最新有效
        决策计入一次；已付款金额不会被自动倒扣。
        """

        rows = conn.execute(
            "SELECT latest_decision, latest_ratio FROM br_evidence "
            "WHERE right_id=? AND latest_decision IN ('accepted','partial')",
            (fee_row["right_id"],),
        ).fetchall()
        target = sum(fee_row["amount_minor"] * row["latest_ratio"] // 100 for row in rows)
        target = max(target, fee_row["paid_minor"])
        if fee_row["status"] == "frozen":
            conn.execute("UPDATE br_fee_items SET payable_minor=? WHERE fee_id=?",
                         (target, fee_row["fee_id"]))
            return
        if target > fee_row["paid_minor"]:
            new_status = "payable"
        elif target > 0:
            new_status = "paid"
        else:
            new_status = "planned"
        conn.execute("UPDATE br_fee_items SET payable_minor=?, status=? WHERE fee_id=?",
                     (target, new_status, fee_row["fee_id"]))

    def _recompute_right_acceptance_fees(self, conn, right_id: str) -> None:
        fees = conn.execute(
            "SELECT * FROM br_fee_items WHERE right_id=? AND due_kind='per_acceptance' "
            "AND status!='void'", (right_id,)
        ).fetchall()
        for fee in fees:
            self._recompute_acceptance_payable(conn, fee)

    def _open_dispute(self, conn, *, actor: Actor, right, evidence, reason: str,
                      accepted_ratio: int, note: str) -> str:
        dispute_id = uuid.uuid4().hex
        reason = self._text(reason, "reason", 500)
        # 争议只冻结与该权益关联的费用，其他权益照常结算
        fees = conn.execute(
            "SELECT * FROM br_fee_items WHERE right_id=? AND status IN ('planned','payable')",
            (right["right_id"],)
        ).fetchall()
        first_fee_id = fees[0]["fee_id"] if fees else None
        conn.execute(
            "INSERT INTO br_disputes(dispute_id,right_id,evidence_id,fee_id,reason,status,"
            "raised_by,raised_at) VALUES(?,?,?,?,?, 'open', ?, ?)",
            (dispute_id, right["right_id"], evidence["evidence_id"], first_fee_id, reason,
             actor.actor_id, self._now()),
        )
        for fee in fees:
            conn.execute(
                "UPDATE br_fee_items SET status='frozen', prev_status=?, frozen_for_dispute_id=? "
                "WHERE fee_id=? AND status!='frozen'",
                (fee["status"], dispute_id, fee["fee_id"]),
            )
        return dispute_id

    # ------------------------------------------------------------------
    # 争议处理
    # ------------------------------------------------------------------

    def resolve_dispute(self, *, request_id: str, actor_id: str, dispute_id: str,
                        resolution_note: str, granted_ratio: int) -> WriteReceipt:
        payload = {"actor_id": actor_id, "dispute_id": dispute_id,
                   "resolution_note": resolution_note, "granted_ratio": granted_ratio}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._can(actor, "dispute.resolve")
            dispute = conn.execute("SELECT * FROM br_disputes WHERE dispute_id=?",
                                   (dispute_id,)).fetchone()
            if dispute is None:
                raise NotFoundError("争议不存在")
            if dispute["status"] != "open":
                raise ConflictError("争议已经处理")
            right = self._right_row(conn, dispute["right_id"])
            agreement = self._agreement_row(conn, right["agreement_id"])
            self._scope_access(actor, agreement["organization_id"])
            if not isinstance(granted_ratio, int) or not (0 <= granted_ratio <= 100):
                raise ValidationError("granted_ratio 必须在 0 到 100 之间")
            resolution_note = str(resolution_note or "")[:1000]

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                evidence = conn.execute(
                    "SELECT * FROM br_evidence WHERE evidence_id=?", (dispute["evidence_id"],)
                ).fetchone()
                # 先写入裁决再统一重算应付
                conn.execute(
                    "UPDATE br_disputes SET status='resolved', resolution_note=?, resolved_by=?, "
                    "resolved_at=? WHERE dispute_id=?",
                    (resolution_note, actor_id, now, dispute_id),
                )
                if evidence is not None:
                    acceptance_id = uuid.uuid4().hex
                    final_decision = "returned" if granted_ratio == 0 else (
                        "accepted" if granted_ratio == 100 else "partial")
                    conn.execute(
                        "INSERT INTO br_acceptance_events(acceptance_id,evidence_id,right_id,decision,"
                        "accepted_ratio,note,decided_by,decided_at,supersedes_acceptance_id,dispute_id) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (acceptance_id, evidence["evidence_id"], dispute["right_id"], final_decision,
                         granted_ratio, f"争议裁决：{resolution_note}", actor_id, now,
                         evidence["latest_acceptance_id"], dispute_id),
                    )
                    conn.execute(
                        "UPDATE br_evidence SET latest_decision=?, latest_ratio=?, "
                        "latest_acceptance_id=? WHERE evidence_id=?",
                        (final_decision, granted_ratio, acceptance_id, evidence["evidence_id"]),
                    )
                    right_status = {0: "planned", 100: "fulfilled"}.get(granted_ratio, "partial")
                    conn.execute("UPDATE br_rights SET status=? WHERE right_id=? AND status='disputed'",
                                 (right_status, dispute["right_id"]))
                # 重算按验收计费的应付（冻结状态下只更新基数，不改状态）
                self._recompute_right_acceptance_fees(conn, dispute["right_id"])
                # 该权益没有其他未决争议时，解冻其全部被冻结费用；其他权益不受影响
                remaining = conn.execute(
                    "SELECT COUNT(*) AS c FROM br_disputes WHERE right_id=? AND status='open' "
                    "AND dispute_id!=?", (dispute["right_id"], dispute_id)
                ).fetchone()["c"]
                if remaining == 0:
                    for fee in conn.execute(
                        "SELECT * FROM br_fee_items WHERE right_id=? AND status='frozen'",
                        (dispute["right_id"],)
                    ).fetchall():
                        if fee["payable_minor"] > fee["paid_minor"]:
                            restored = "payable"
                        elif fee["payable_minor"] > 0:
                            restored = "paid"
                        else:
                            restored = "planned"
                        conn.execute(
                            "UPDATE br_fee_items SET status=?, frozen_for_dispute_id=NULL, "
                            "prev_status=NULL WHERE fee_id=?",
                            (restored, fee["fee_id"]),
                        )
                self._audit(conn, actor_id=actor_id, action="brand.dispute.resolved",
                            resource_type="br_dispute", resource_id=dispute_id,
                            detail={"granted_ratio": granted_ratio,
                                    "resolution_note": resolution_note})
                return "br_dispute", dispute_id, {"dispute_id": dispute_id,
                                                  "granted_ratio": granted_ratio}

            return self._idempotent(conn, request_id=request_id, action="brand.resolve_dispute",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 付款与逐笔解释
    # ------------------------------------------------------------------

    def propose_payment(self, *, request_id: str, actor_id: str, agreement_id: str,
                        amount_minor: int, currency: str, memo: str = "",
                        allocations: list[dict[str, Any]] | None = None) -> WriteReceipt:
        allocations = allocations or []
        payload = {"actor_id": actor_id, "agreement_id": agreement_id,
                   "amount_minor": amount_minor, "currency": currency, "memo": memo,
                   "allocations": allocations}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._can(actor, "payment.write")
            agreement = self._agreement_row(conn, agreement_id)
            self._scope_access(actor, agreement["organization_id"])
            if not isinstance(amount_minor, int) or amount_minor <= 0:
                raise ValidationError("amount_minor 必须是正整数")
            currency = self._text(currency, "currency", 3)
            if not allocations:
                raise ValidationError("付款必须包含至少一条权益分配")
            total = 0
            normalized: list[dict[str, Any]] = []
            for item in allocations:
                fee_id = self._id(item["fee_id"], "fee_id")
                amount = item["amount_minor"]
                if not isinstance(amount, int) or amount <= 0:
                    raise ValidationError("分配金额必须是正整数")
                fee = conn.execute("SELECT * FROM br_fee_items WHERE fee_id=?", (fee_id,)).fetchone()
                if fee is None or fee["agreement_id"] != agreement_id:
                    raise NotFoundError("费用不属于该协议")
                if fee["status"] == "frozen":
                    raise ConflictError(f"费用 {fee_id} 因争议冻结，不能付款")
                if fee["currency"] != currency:
                    raise ValidationError("付款币种必须与费用一致")
                evidence_id = item.get("evidence_id")
                acceptance_id = None
                if evidence_id:
                    ev = conn.execute("SELECT * FROM br_evidence WHERE evidence_id=?",
                                      (evidence_id,)).fetchone()
                    if ev is None or ev["right_id"] != fee["right_id"]:
                        raise ValidationError("凭证与费用对应的权益不匹配")
                    if ev["latest_decision"] not in ("accepted", "partial"):
                        raise ConflictError("只有通过或部分验收的凭证可以支撑付款")
                    acceptance_id = ev["latest_acceptance_id"]
                total += amount
                normalized.append({"fee_id": fee_id, "right_id": fee["right_id"],
                                   "amount_minor": amount, "evidence_id": evidence_id,
                                   "acceptance_id": acceptance_id})
            if total != amount_minor:
                raise ValidationError("分配金额之和必须等于付款金额")
            # 同币种、同费用的可付余额校验
            for item in normalized:
                fee = conn.execute("SELECT * FROM br_fee_items WHERE fee_id=?",
                                   (item["fee_id"],)).fetchone()
                available = fee["payable_minor"] - fee["paid_minor"]
                if item["amount_minor"] > available:
                    raise ConflictError(f"费用 {item['fee_id']} 可付余额不足")

            def create() -> tuple[str, str, dict[str, Any]]:
                payment_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO br_payments(payment_id,agreement_id,amount_minor,currency,status,"
                    "memo,created_by,created_at) VALUES(?,?,?,?,'proposed',?,?,?)",
                    (payment_id, agreement_id, amount_minor, currency,
                     str(memo or "")[:500], actor_id, self._now()),
                )
                for item in normalized:
                    conn.execute(
                        "INSERT INTO br_payment_allocations(payment_id,fee_id,right_id,amount_minor,"
                        "evidence_id,acceptance_id) VALUES(?,?,?,?,?,?)",
                        (payment_id, item["fee_id"], item["right_id"], item["amount_minor"],
                         item["evidence_id"], item["acceptance_id"]),
                    )
                self._audit(conn, actor_id=actor_id, action="brand.payment.proposed",
                            resource_type="br_payment", resource_id=payment_id,
                            detail={"agreement_id": agreement_id, "amount_minor": amount_minor,
                                    "currency": currency, "allocations": len(normalized)})
                return "br_payment", payment_id, {"payment_id": payment_id}

            return self._idempotent(conn, request_id=request_id, action="brand.propose_payment",
                                    payload=payload, create=create)

    def complete_payment(self, *, request_id: str, actor_id: str, payment_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "payment_id": payment_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._can(actor, "payment.write")
            payment = conn.execute("SELECT * FROM br_payments WHERE payment_id=?",
                                   (payment_id,)).fetchone()
            if payment is None:
                raise NotFoundError("付款单不存在")
            agreement = self._agreement_row(conn, payment["agreement_id"])
            self._scope_access(actor, agreement["organization_id"])
            if payment["status"] == "completed":
                raise ConflictError("付款已经完成")
            allocations = conn.execute(
                "SELECT * FROM br_payment_allocations WHERE payment_id=?", (payment_id,)
            ).fetchall()
            # 付款前最后一次冻结检查
            for alloc in allocations:
                fee = conn.execute("SELECT * FROM br_fee_items WHERE fee_id=?",
                                   (alloc["fee_id"],)).fetchone()
                if fee["status"] == "frozen":
                    raise ConflictError(f"费用 {alloc['fee_id']} 已被争议冻结，付款中止")

            def create() -> tuple[str, str, dict[str, Any]]:
                for alloc in allocations:
                    conn.execute(
                        "UPDATE br_fee_items SET paid_minor = paid_minor + ? WHERE fee_id=?",
                        (alloc["amount_minor"], alloc["fee_id"]),
                    )
                    fee = conn.execute("SELECT * FROM br_fee_items WHERE fee_id=?",
                                       (alloc["fee_id"],)).fetchone()
                    if fee["paid_minor"] >= fee["payable_minor"] and fee["status"] == "payable":
                        conn.execute("UPDATE br_fee_items SET status='paid' WHERE fee_id=?",
                                     (alloc["fee_id"],))
                conn.execute("UPDATE br_payments SET status='completed' WHERE payment_id=?",
                             (payment_id,))
                self._audit(conn, actor_id=actor_id, action="brand.payment.completed",
                            resource_type="br_payment", resource_id=payment_id,
                            detail={"amount_minor": payment["amount_minor"],
                                    "currency": payment["currency"]})
                return "br_payment", payment_id, {"payment_id": payment_id, "status": "completed"}

            return self._idempotent(conn, request_id=request_id, action="brand.complete_payment",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 查询：按角色裁剪的明细
    # ------------------------------------------------------------------

    def _right_view(self, conn, row) -> dict[str, Any]:
        ex = conn.execute("SELECT * FROM br_exclusivity WHERE right_id=?",
                          (row["right_id"],)).fetchone()
        view = {
            "right_id": row["right_id"], "agreement_id": row["agreement_id"],
            "version_no": row["version_no"], "event_type": row["event_type"],
            "right_kind": row["right_kind"], "title": row["title"],
            "region": row["region"], "venue": row["venue"],
            "window_start": row["window_start"], "window_end": row["window_end"],
            "spec": json.loads(row["spec_json"]), "status": row["status"],
            "copied_from_right_id": row["copied_from_right_id"],
            "exclusivity": None,
        }
        if ex:
            view["exclusivity"] = {
                "category_label": ex["category_label"], "scope_type": ex["scope_type"],
                "scope_value": ex["scope_value"], "window_start": ex["window_start"],
                "window_end": ex["window_end"],
            }
        return view

    def list_agreements(self, *, actor_id: str) -> list[dict[str, Any]]:
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._can_read(actor, "agreement")
            sql = "SELECT * FROM br_agreements"
            parameters: list[Any] = []
            if actor.role != "admin" and actor.role != "auditor":
                sql += " WHERE organization_id=?"
                parameters.append(actor.organization_id)
            sql += " ORDER BY created_at, agreement_id"
            return [dict(row) for row in conn.execute(sql, parameters)]

    def get_agreement_detail(self, *, actor_id: str, agreement_id: str) -> dict[str, Any]:
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._can_read(actor, "agreement")
            agreement = self._agreement_row(conn, agreement_id)
            self._scope_access(actor, agreement["organization_id"])
            versions = [dict(row) for row in conn.execute(
                "SELECT agreement_id,version_no,status,document_ref,content_hash,change_note,"
                "signed_by,signed_at,created_at FROM br_agreement_versions "
                "WHERE agreement_id=? ORDER BY version_no", (agreement_id,))]
            rights = [self._right_view(conn, row) for row in conn.execute(
                "SELECT * FROM br_rights WHERE agreement_id=? ORDER BY version_no, created_at",
                (agreement_id,))]
            detail: dict[str, Any] = {
                "agreement": dict(agreement), "versions": versions, "rights": rights,
                "visible_to_role": actor.role,
            }
            if permissions.can_read(actor.role, "operations"):
                detail["sessions"] = [dict(row) for row in conn.execute(
                    "SELECT * FROM br_sessions WHERE agreement_id=? ORDER BY starts_at",
                    (agreement_id,))]
                detail["media_assets"] = [dict(row) for row in conn.execute(
                    "SELECT m.* FROM br_media_assets m JOIN br_rights r ON r.right_id=m.right_id "
                    "WHERE r.agreement_id=? ORDER BY m.created_at", (agreement_id,))]
                detail["deliverables"] = [dict(row) for row in conn.execute(
                    "SELECT d.* FROM br_deliverables d JOIN br_rights r ON r.right_id=d.right_id "
                    "WHERE r.agreement_id=? ORDER BY d.created_at", (agreement_id,))]
            if permissions.can_read(actor.role, "dispute"):
                detail["disputes"] = [dict(row) for row in conn.execute(
                    "SELECT * FROM br_disputes WHERE right_id IN "
                    "(SELECT right_id FROM br_rights WHERE agreement_id=?) "
                    "ORDER BY raised_at", (agreement_id,))]
            if permissions.can_read(actor.role, "finance"):
                detail["fees"] = [dict(row) for row in conn.execute(
                    "SELECT * FROM br_fee_items WHERE agreement_id=? ORDER BY created_at",
                    (agreement_id,))]
            else:
                # 非财务角色只能看到费用状态，看不到金额
                detail["fee_status"] = [
                    {"fee_id": row["fee_id"], "right_id": row["right_id"],
                     "due_kind": row["due_kind"], "status": row["status"],
                     "currency": row["currency"]}
                    for row in conn.execute(
                        "SELECT * FROM br_fee_items WHERE agreement_id=? ORDER BY created_at",
                        (agreement_id,))]
            return detail

    def list_calendar(self, *, actor_id: str, region: str | None = None,
                      venue: str | None = None) -> list[dict[str, Any]]:
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._can_read(actor, "operations")
            sql = ("SELECT s.*, r.title AS right_title, r.event_type, a.partner_name "
                   "FROM br_sessions s JOIN br_rights r ON r.right_id=s.source_right_id "
                   "JOIN br_agreements a ON a.agreement_id=s.agreement_id WHERE 1=1")
            parameters: list[Any] = []
            if actor.role not in ("admin", "auditor"):
                sql += " AND s.agreement_id IN (SELECT agreement_id FROM br_agreements "
                sql += "WHERE organization_id=?)"
                parameters.append(actor.organization_id)
            if region:
                sql += " AND s.region=?"
                parameters.append(region)
            if venue:
                sql += " AND s.venue=?"
                parameters.append(venue)
            sql += " ORDER BY s.starts_at"
            return [dict(row) for row in conn.execute(sql, parameters)]

    def list_evidence(self, *, actor_id: str, right_id: str | None = None) -> list[dict[str, Any]]:
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._can_read(actor, "operations")
            sql = ("SELECT e.*, r.agreement_id FROM br_evidence e "
                   "JOIN br_rights r ON r.right_id=e.right_id WHERE 1=1")
            parameters: list[Any] = []
            if actor.role not in ("admin", "auditor"):
                sql += " AND r.agreement_id IN (SELECT agreement_id FROM br_agreements WHERE organization_id=?)"
                parameters.append(actor.organization_id)
            if right_id:
                sql += " AND e.right_id=?"
                parameters.append(right_id)
            sql += " ORDER BY e.uploaded_at"
            return [dict(row) for row in conn.execute(sql, parameters)]

    def get_payment_explanation(self, *, actor_id: str, payment_id: str) -> dict[str, Any]:
        """解释一笔付款对应哪些有效权益、验收证据与冻结情况。"""

        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._can_read(actor, "finance")
            payment = conn.execute("SELECT * FROM br_payments WHERE payment_id=?",
                                   (payment_id,)).fetchone()
            if payment is None:
                raise NotFoundError("付款单不存在")
            agreement = self._agreement_row(conn, payment["agreement_id"])
            self._scope_access(actor, agreement["organization_id"])
            allocations_view = []
            for alloc in conn.execute(
                "SELECT * FROM br_payment_allocations WHERE payment_id=? "
                "ORDER BY rowid", (payment_id,)
            ):
                right = self._right_row(conn, alloc["right_id"])
                fee = conn.execute("SELECT * FROM br_fee_items WHERE fee_id=?",
                                   (alloc["fee_id"],)).fetchone()
                item: dict[str, Any] = {
                    "fee_id": alloc["fee_id"], "fee_label": fee["label"],
                    "right_id": alloc["right_id"], "right_title": right["title"],
                    "right_kind": right["right_kind"], "event_type": right["event_type"],
                    "region": right["region"], "venue": right["venue"],
                    "window_start": right["window_start"], "window_end": right["window_end"],
                    "amount_minor": alloc["amount_minor"],
                    "evidence": None, "fee_status": fee["status"],
                }
                if alloc["evidence_id"]:
                    ev = conn.execute("SELECT * FROM br_evidence WHERE evidence_id=?",
                                      (alloc["evidence_id"],)).fetchone()
                    acceptance = conn.execute(
                        "SELECT * FROM br_acceptance_events WHERE acceptance_id=?",
                        (alloc["acceptance_id"],)
                    ).fetchone()
                    item["evidence"] = {
                        "evidence_id": ev["evidence_id"], "evidence_kind": ev["evidence_kind"],
                        "file_ref": ev["file_ref"], "content_hash": ev["content_hash"],
                        "decision": acceptance["decision"] if acceptance else ev["latest_decision"],
                        "accepted_ratio": acceptance["accepted_ratio"] if acceptance else ev["latest_ratio"],
                        "decided_at": acceptance["decided_at"] if acceptance else None,
                        "note": acceptance["note"] if acceptance else "",
                    }
                allocations_view.append(item)
            open_disputes = conn.execute(
                "SELECT d.dispute_id, d.reason, d.fee_id FROM br_disputes d WHERE d.status='open' "
                "AND d.right_id IN (SELECT right_id FROM br_payment_allocations WHERE payment_id=?)",
                (payment_id,)
            ).fetchall()
            return {
                "payment": dict(payment),
                "partner_name": agreement["partner_name"],
                "total_explained_minor": sum(a["amount_minor"] for a in allocations_view),
                "allocations": allocations_view,
                "open_disputes": [dict(row) for row in open_disputes],
            }
