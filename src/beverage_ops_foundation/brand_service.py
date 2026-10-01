"""品牌权益履约领域服务：协议版本、计划冲突、验收争议与结算解释。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from .audit import append_event
from .brand_models import BrandConflict
from .errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .service import DomainService

EVENT_TYPES = frozenset({"racing", "football", "music"})
ENTITLEMENT_KINDS = frozenset({"exposure_position", "media_asset", "activity_window", "supply"})
ATTACHMENT_KINDS = frozenset({"contract", "exposure", "exclusivity", "supply", "material", "fee", "activity", "other"})
PLAN_WRITE_ROLES = frozenset({"admin", "legal", "marketing"})
LEGAL_ROLES = frozenset({"admin", "legal"})
MARKET_ROLES = frozenset({"admin", "marketing", "reviewer", "operator"})
FINANCE_ROLES = frozenset({"admin", "finance"})
READ_ALL_ROLES = frozenset({"admin", "auditor", "legal"})


class BrandConflictError(DomainError):
    """签署或变更前发现时间与范围冲突。"""

    code = "brand_conflict"
    status = 409

    def __init__(self, conflicts: list[BrandConflict]) -> None:
        super().__init__("品牌权益计划存在冲突，不能签署或变更")
        self.conflicts = conflicts


def parse_instant(value: str, field: str) -> datetime:
    """解析必须带时区的 ISO 8601 时间。"""

    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} 必须是带时区的 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须包含时区")
    return parsed


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def region_overlaps(a: str, b: str) -> bool:
    return a == b or a == "*" or b == "*"


def _region_code(value: str, validator) -> str:
    text = str(value).strip()
    if text == "*":
        return "*"
    return validator(text, "region_code")


class BrandService(DomainService):
    """在基础服务之上协调品牌权益的计划、履约、争议和结算。"""

    # ------------------------------------------------------------------ 读取辅助

    def _brand_actor(self, connection, actor_id: str):
        return self._actor(connection, actor_id)

    def _agreement_row(self, connection, agreement_id: str):
        row = connection.execute(
            "SELECT * FROM brand_agreements WHERE agreement_id=?", (agreement_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("品牌协议不存在")
        return row

    def _version_row(self, connection, agreement_id: str, version_no: int):
        row = connection.execute(
            "SELECT * FROM brand_agreement_versions WHERE agreement_id=? AND version_no=?",
            (agreement_id, version_no),
        ).fetchone()
        if row is None:
            raise NotFoundError("协议版本不存在")
        return row

    def _draft_version(self, connection, agreement_id: str):
        agreement = self._agreement_row(connection, agreement_id)
        row = connection.execute(
            "SELECT * FROM brand_agreement_versions WHERE agreement_id=? AND version_no=?",
            (agreement_id, agreement["current_version"]),
        ).fetchone()
        if row is None or row["status"] != "draft":
            raise ValidationError("只有未签署的草稿版本可以修改")
        return agreement, row

    def _signed_version_no(self, connection, agreement_id: str) -> int:
        agreement = self._agreement_row(connection, agreement_id)
        if agreement["status"] != "active":
            raise ValidationError("协议尚未签署生效")
        return agreement["current_version"]

    def _entitlement_row(self, connection, agreement_id: str, version_no: int, entitlement_id: str):
        row = connection.execute(
            "SELECT * FROM brand_entitlements WHERE agreement_id=? AND version_no=? AND entitlement_id=?",
            (agreement_id, version_no, entitlement_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("权益不存在")
        return row

    def _window(self, connection, ent) -> tuple[datetime, datetime]:
        if ent["starts_at"] and ent["ends_at"]:
            return parse_instant(ent["starts_at"], "starts_at"), parse_instant(ent["ends_at"], "ends_at")
        if ent["session_id"]:
            session = connection.execute(
                "SELECT * FROM brand_sessions WHERE agreement_id=? AND version_no=? AND session_id=?",
                (ent["agreement_id"], ent["version_no"], ent["session_id"]),
            ).fetchone()
            if session is not None:
                return parse_instant(session["starts_at"], "starts_at"), parse_instant(session["ends_at"], "ends_at")
        raise ValidationError(f"权益 {ent['entitlement_id']} 缺少有效的时间窗口（需要自有窗口或绑定场次）")

    # ------------------------------------------------------------------ 协议与版本

    def create_agreement(self, *, request_id: str, actor_id: str, agreement_id: str,
                         partner_id: str, partner_name: str,
                         renewal_of_agreement_id: str | None = None) -> Any:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "partner_id": partner_id,
                   "partner_name": partner_name, "renewal_of_agreement_id": renewal_of_agreement_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._brand_actor(connection, actor_id)
            self._require(actor, *LEGAL_ROLES)
            agreement_id = self._identifier(agreement_id, "agreement_id")
            partner_id = self._identifier(partner_id, "partner_id")
            partner_name = self._text(partner_name, "partner_name")
            predecessor_id = None
            if renewal_of_agreement_id:
                predecessor_id = self._identifier(renewal_of_agreement_id, "renewal_of_agreement_id")
                predecessor = connection.execute(
                    "SELECT * FROM brand_agreements WHERE agreement_id=?", (predecessor_id,)
                ).fetchone()
                if predecessor is None:
                    raise NotFoundError("被续期的原协议不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                try:
                    connection.execute(
                        "INSERT INTO brand_agreements(agreement_id,partner_id,partner_name,current_version,"
                        "status,renewal_of_agreement_id,created_by,created_at) VALUES(?,?,?,?, 'draft',?,?,?)",
                        (agreement_id, partner_id, partner_name, 1, predecessor_id, actor_id, now),
                    )
                    connection.execute(
                        "INSERT INTO brand_agreement_versions(agreement_id,version_no,status,change_note,"
                        "created_by,created_at) VALUES(?,1,'draft','初始版本',?,?)",
                        (agreement_id, actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("协议编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="brand_agreement.created",
                             resource_type="brand_agreement", resource_id=agreement_id,
                             detail={"partner_id": partner_id, "partner_name": partner_name,
                                     "renewal_of_agreement_id": predecessor_id}, occurred_at=now)
                return "brand_agreement", agreement_id, {"agreement_id": agreement_id, "version_no": 1}

            return self._idempotent(connection, request_id=request_id,
                                    action="brand_create_agreement", payload=payload, create=create)

    def revise_agreement(self, *, request_id: str, actor_id: str, agreement_id: str,
                         change_note: str) -> Any:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "change_note": change_note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._brand_actor(connection, actor_id)
            self._require(actor, *LEGAL_ROLES)
            agreement = self._agreement_row(connection, agreement_id)
            if agreement["status"] != "active":
                raise ValidationError("只有已签署生效的协议才能发起变更")
            base_no = agreement["current_version"]
            base = self._version_row(connection, agreement_id, base_no)
            if base["status"] != "signed":
                raise ValidationError("当前版本不是已签署版本")
            change_note = self._text(change_note, "change_note")
            new_no = base_no + 1

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                connection.execute(
                    "INSERT INTO brand_agreement_versions(agreement_id,version_no,status,change_note,"
                    "created_by,created_at) VALUES(?,?,'draft',?,?,?)",
                    (agreement_id, new_no, change_note, actor_id, now),
                )
                connection.execute(
                    "UPDATE brand_agreements SET current_version=? WHERE agreement_id=?",
                    (new_no, agreement_id),
                )
                for table, columns in (
                    ("brand_sessions", "session_id,event_type,name,region_code,venue,starts_at,ends_at"),
                    ("brand_entitlements", "entitlement_id,session_id,region_code,kind,placement_code,asset_code,"
                                           "zone_code,category_code,exclusive,starts_at,ends_at,quantity,unit,title"),
                    ("brand_fee_commitments", "fee_id,entitlement_id,title,amount_minor,currency,due_at"),
                ):
                    rows = connection.execute(
                        f"SELECT {columns} FROM {table} WHERE agreement_id=? AND version_no=?",
                        (agreement_id, base_no),
                    ).fetchall()
                    placeholders = ",".join("?" for _ in columns.split(","))
                    for row in rows:
                        connection.execute(
                            f"INSERT INTO {table}(agreement_id,version_no,{columns}) "
                            f"VALUES(?, ?, {placeholders})",
                            (agreement_id, new_no, *[row[key] for key in columns.split(",")]),
                        )
                # 物料交付状态不跨版本继承：新计划重新交付
                materials = connection.execute(
                    "SELECT delivery_id,session_id,entitlement_id,title,quantity,unit,due_at "
                    "FROM brand_material_deliveries WHERE agreement_id=? AND version_no=?",
                    (agreement_id, base_no),
                ).fetchall()
                for row in materials:
                    connection.execute(
                        "INSERT INTO brand_material_deliveries(agreement_id,version_no,delivery_id,session_id,"
                        "entitlement_id,title,quantity,unit,due_at,status) VALUES(?,?,?,?,?,?,?,?,?, 'pending')",
                        (agreement_id, new_no, row["delivery_id"], row["session_id"], row["entitlement_id"],
                         row["title"], row["quantity"], row["unit"], row["due_at"]),
                    )
                append_event(connection, actor_id=actor_id, action="brand_agreement.revision_created",
                             resource_type="brand_agreement", resource_id=agreement_id,
                             detail={"base_version": base_no, "new_version": new_no, "change_note": change_note},
                             occurred_at=now)
                return "brand_agreement", agreement_id, {"agreement_id": agreement_id, "version_no": new_no}

            return self._idempotent(connection, request_id=request_id,
                                    action="brand_revise_agreement", payload=payload, create=create)

    def sign_version(self, *, request_id: str, actor_id: str, agreement_id: str,
                     version_no: int, signer_name: str) -> Any:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "version_no": version_no}
        with self.database.transaction(immediate=True) as connection:
            actor = self._brand_actor(connection, actor_id)
            self._require(actor, *LEGAL_ROLES)
            agreement = self._agreement_row(connection, agreement_id)
            version = self._version_row(connection, agreement_id, int(version_no))
            if version["status"] != "draft":
                raise ValidationError("只能签署草稿版本")
            if agreement["current_version"] != version["version_no"]:
                raise ValidationError("只能签署最新版本")
            self._text(signer_name, "signer_name")
            self._validate_plan(connection, agreement_id, version["version_no"])
            conflicts = self._scan_conflicts(connection, agreement_id, version["version_no"])
            if conflicts:
                raise BrandConflictError(conflicts)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                if agreement["status"] == "active":
                    connection.execute(
                        "UPDATE brand_agreement_versions SET status='superseded' "
                        "WHERE agreement_id=? AND status='signed'",
                        (agreement_id,),
                    )
                connection.execute(
                    "UPDATE brand_agreement_versions SET status='signed',signed_by=?,signed_at=? "
                    "WHERE agreement_id=? AND version_no=?",
                    (actor_id, now, agreement_id, version["version_no"]),
                )
                connection.execute(
                    "UPDATE brand_agreements SET status='active' WHERE agreement_id=?", (agreement_id,)
                )
                append_event(connection, actor_id=actor_id, action="brand_version.signed",
                             resource_type="brand_agreement", resource_id=agreement_id,
                             detail={"version_no": version["version_no"], "signer_name": signer_name},
                             occurred_at=now)
                return "brand_agreement", agreement_id, {"agreement_id": agreement_id,
                                                         "version_no": version["version_no"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="brand_sign_version", payload=payload, create=create)

    # ------------------------------------------------------------------ 计划条目

    def _validate_plan(self, connection, agreement_id: str, version_no: int) -> None:
        sessions = {row["session_id"]: row for row in connection.execute(
            "SELECT * FROM brand_sessions WHERE agreement_id=? AND version_no=?", (agreement_id, version_no))}
        entitlements = connection.execute(
            "SELECT * FROM brand_entitlements WHERE agreement_id=? AND version_no=?",
            (agreement_id, version_no)).fetchall()
        if not entitlements:
            raise ValidationError("版本至少需要一项品牌权益")
        for ent in entitlements:
            if ent["session_id"] and ent["session_id"] not in sessions:
                raise ValidationError(f"权益 {ent['entitlement_id']} 引用了不存在的场次")
            start, end = self._window(connection, ent)
            if end <= start:
                raise ValidationError(f"权益 {ent['entitlement_id']} 的结束时间必须晚于开始时间")
            if ent["exclusive"] and not ent["category_code"]:
                raise ValidationError(f"排他权益 {ent['entitlement_id']} 必须声明排他品类")
            if ent["kind"] == "supply" and (ent["quantity"] is None or ent["quantity"] <= 0):
                raise ValidationError(f"供货权益 {ent['entitlement_id']} 必须约定正数供货数量")
        fee_rows = connection.execute(
            "SELECT * FROM brand_fee_commitments WHERE agreement_id=? AND version_no=?",
            (agreement_id, version_no)).fetchall()
        ent_ids = {row["entitlement_id"] for row in entitlements}
        for fee in fee_rows:
            if fee["entitlement_id"] not in ent_ids:
                raise ValidationError(f"费用 {fee['fee_id']} 关联了不存在的权益")
        for material in connection.execute(
            "SELECT * FROM brand_material_deliveries WHERE agreement_id=? AND version_no=?",
                (agreement_id, version_no)):
            if material["session_id"] and material["session_id"] not in sessions:
                raise ValidationError(f"物料 {material['delivery_id']} 引用了不存在的场次")
            if material["entitlement_id"] and material["entitlement_id"] not in ent_ids:
                raise ValidationError(f"物料 {material['delivery_id']} 引用了不存在的权益")

    def add_session(self, *, request_id: str, actor_id: str, agreement_id: str, session_id: str,
                    event_type: str, name: str, region_code: str, venue: str,
                    starts_at: str, ends_at: str) -> Any:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "session_id": session_id,
                   "event_type": event_type, "name": name, "region_code": region_code, "venue": venue,
                   "starts_at": starts_at, "ends_at": ends_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._brand_actor(connection, actor_id)
            self._require(actor, *PLAN_WRITE_ROLES)
            self._draft_version(connection, agreement_id)
            session_id = self._identifier(session_id, "session_id")
            if event_type not in EVENT_TYPES:
                raise ValidationError("event_type 必须是 racing、football 或 music")
            name = self._text(name, "name")
            region_code = _region_code(region_code, self._identifier)
            venue = self._text(venue, "venue")
            start = parse_instant(starts_at, "starts_at")
            end = parse_instant(ends_at, "ends_at")
            if end <= start:
                raise ValidationError("场次结束时间必须晚于开始时间")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                try:
                    connection.execute(
                        "INSERT INTO brand_sessions(agreement_id,version_no,session_id,event_type,name,"
                        "region_code,venue,starts_at,ends_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (agreement_id, self._draft_version_no(connection, agreement_id), session_id, event_type,
                         name, region_code, venue, starts_at, ends_at),
                    )
                except Exception as exc:
                    raise ConflictError("场次编号在该版本中已经存在") from exc
                append_event(connection, actor_id=actor_id, action="brand_session.planned",
                             resource_type="brand_session", resource_id=f"{agreement_id}/{session_id}",
                             detail={"event_type": event_type, "region_code": region_code,
                                     "starts_at": starts_at, "ends_at": ends_at}, occurred_at=now)
                return "brand_session", session_id, {"session_id": session_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="brand_add_session", payload=payload, create=create)

    def _draft_version_no(self, connection, agreement_id: str) -> int:
        return connection.execute(
            "SELECT current_version FROM brand_agreements WHERE agreement_id=?", (agreement_id,)
        ).fetchone()["current_version"]

    def add_entitlement(self, *, request_id: str, actor_id: str, agreement_id: str, entitlement_id: str,
                        kind: str, title: str, region_code: str, session_id: str | None = None,
                        placement_code: str | None = None, asset_code: str | None = None,
                        zone_code: str | None = None, category_code: str | None = None,
                        exclusive: bool = False, starts_at: str | None = None, ends_at: str | None = None,
                        quantity: float | None = None, unit: str | None = None) -> Any:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "entitlement_id": entitlement_id,
                   "kind": kind, "title": title, "region_code": region_code, "session_id": session_id,
                   "placement_code": placement_code, "asset_code": asset_code, "zone_code": zone_code,
                   "category_code": category_code, "exclusive": bool(exclusive),
                   "starts_at": starts_at, "ends_at": ends_at, "quantity": quantity, "unit": unit}
        with self.database.transaction(immediate=True) as connection:
            actor = self._brand_actor(connection, actor_id)
            self._require(actor, *PLAN_WRITE_ROLES)
            self._draft_version(connection, agreement_id)
            entitlement_id = self._identifier(entitlement_id, "entitlement_id")
            if kind not in ENTITLEMENT_KINDS:
                raise ValidationError("kind 不在允许范围内")
            title = self._text(title, "title")
            region_code = _region_code(region_code, self._identifier)
            if session_id:
                session_id = self._identifier(session_id, "session_id")
            optional_codes: dict[str, str | None] = {}
            for code_field, code_value in (("placement_code", placement_code), ("asset_code", asset_code),
                                           ("zone_code", zone_code), ("category_code", category_code),
                                           ("unit", unit)):
                optional_codes[code_field] = (
                    self._text(code_value, code_field, 80) if code_value is not None else None)
            if bool(starts_at) != bool(ends_at):
                raise ValidationError("starts_at 与 ends_at 必须同时提供")
            if starts_at:
                start = parse_instant(starts_at, "starts_at")
                end = parse_instant(ends_at, "ends_at")
                if end <= start:
                    raise ValidationError("权益结束时间必须晚于开始时间")
            if quantity is not None and quantity <= 0:
                raise ValidationError("quantity 必须为正数")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                version_no = self._draft_version_no(connection, agreement_id)
                try:
                    connection.execute(
                        "INSERT INTO brand_entitlements(agreement_id,version_no,entitlement_id,session_id,"
                        "region_code,kind,placement_code,asset_code,zone_code,category_code,exclusive,"
                        "starts_at,ends_at,quantity,unit,title) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (agreement_id, version_no, entitlement_id, session_id, region_code, kind,
                         optional_codes["placement_code"], optional_codes["asset_code"],
                         optional_codes["zone_code"], optional_codes["category_code"],
                         1 if exclusive else 0, starts_at, ends_at, quantity,
                         optional_codes["unit"], title),
                    )
                except Exception as exc:
                    raise ConflictError("权益编号在该版本中已经存在") from exc
                append_event(connection, actor_id=actor_id, action="brand_entitlement.planned",
                             resource_type="brand_entitlement",
                             resource_id=f"{agreement_id}/{entitlement_id}",
                             detail={"kind": kind, "region_code": region_code, "exclusive": bool(exclusive),
                                     "category_code": optional_codes["category_code"],
                                     "placement_code": optional_codes["placement_code"],
                                     "asset_code": optional_codes["asset_code"],
                                     "zone_code": optional_codes["zone_code"]}, occurred_at=now)
                return "brand_entitlement", entitlement_id, {"entitlement_id": entitlement_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="brand_add_entitlement", payload=payload, create=create)

    def add_fee(self, *, request_id: str, actor_id: str, agreement_id: str, fee_id: str,
                entitlement_id: str, title: str, amount_minor: int, currency: str,
                due_at: str | None = None) -> Any:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "fee_id": fee_id,
                   "entitlement_id": entitlement_id, "title": title, "amount_minor": amount_minor,
                   "currency": currency, "due_at": due_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._brand_actor(connection, actor_id)
            self._require(actor, *PLAN_WRITE_ROLES)
            _, version = self._draft_version(connection, agreement_id)
            fee_id = self._identifier(fee_id, "fee_id")
            entitlement_id = self._identifier(entitlement_id, "entitlement_id")
            title = self._text(title, "title")
            if not isinstance(amount_minor, int) or amount_minor <= 0:
                raise ValidationError("amount_minor 必须是正整数（最小货币单位）")
            currency = self._text(currency, "currency", 8).upper()
            if due_at:
                parse_instant(due_at, "due_at")
            self._entitlement_row(connection, agreement_id, version["version_no"], entitlement_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                try:
                    connection.execute(
                        "INSERT INTO brand_fee_commitments(agreement_id,version_no,fee_id,entitlement_id,"
                        "title,amount_minor,currency,due_at) VALUES(?,?,?,?,?,?,?,?)",
                        (agreement_id, version["version_no"], fee_id, entitlement_id, title,
                         amount_minor, currency, due_at),
                    )
                except Exception as exc:
                    raise ConflictError("费用编号在该版本中已经存在") from exc
                append_event(connection, actor_id=actor_id, action="brand_fee.planned",
                             resource_type="brand_fee", resource_id=f"{agreement_id}/{fee_id}",
                             detail={"entitlement_id": entitlement_id, "currency": currency},
                             occurred_at=now)
                return "brand_fee", fee_id, {"fee_id": fee_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="brand_add_fee", payload=payload, create=create)

    def add_material(self, *, request_id: str, actor_id: str, agreement_id: str, delivery_id: str,
                     title: str, session_id: str | None = None, entitlement_id: str | None = None,
                     quantity: float | None = None, unit: str | None = None,
                     due_at: str | None = None) -> Any:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "delivery_id": delivery_id,
                   "title": title, "session_id": session_id, "entitlement_id": entitlement_id,
                   "quantity": quantity, "unit": unit, "due_at": due_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._brand_actor(connection, actor_id)
            self._require(actor, *PLAN_WRITE_ROLES)
            _, version = self._draft_version(connection, agreement_id)
            delivery_id = self._identifier(delivery_id, "delivery_id")
            title = self._text(title, "title")
            if quantity is not None and quantity <= 0:
                raise ValidationError("quantity 必须为正数")
            if due_at:
                parse_instant(due_at, "due_at")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                try:
                    connection.execute(
                        "INSERT INTO brand_material_deliveries(agreement_id,version_no,delivery_id,session_id,"
                        "entitlement_id,title,quantity,unit,due_at,status) VALUES(?,?,?,?,?,?,?,?,?, 'pending')",
                        (agreement_id, version["version_no"], delivery_id, session_id, entitlement_id,
                         title, quantity, unit, due_at),
                    )
                except Exception as exc:
                    raise ConflictError("物料编号在该版本中已经存在") from exc
                append_event(connection, actor_id=actor_id, action="brand_material.planned",
                             resource_type="brand_material", resource_id=f"{agreement_id}/{delivery_id}",
                             detail={"title": title, "due_at": due_at}, occurred_at=now)
                return "brand_material", delivery_id, {"delivery_id": delivery_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="brand_add_material", payload=payload, create=create)

    def upload_attachment(self, *, request_id: str, actor_id: str, agreement_id: str, version_no: int,
                          kind: str, filename: str, content_hash: str) -> Any:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "version_no": version_no,
                   "kind": kind, "filename": filename, "content_hash": content_hash}
        with self.database.transaction(immediate=True) as connection:
            actor = self._brand_actor(connection, actor_id)
            self._require(actor, *PLAN_WRITE_ROLES)
            version = self._version_row(connection, agreement_id, int(version_no))
            if version["status"] != "draft":
                raise ValidationError("附件只能上传到草稿版本；签署后内容不可变")
            if kind not in ATTACHMENT_KINDS:
                raise ValidationError("附件 kind 不在允许范围内")
            filename = self._text(filename, "filename", 240)
            content_hash = self._text(content_hash, "content_hash", 128)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                existing = connection.execute(
                    "SELECT attachment_id FROM brand_attachments WHERE agreement_id=? AND version_no=? "
                    "AND content_hash=?", (agreement_id, version["version_no"], content_hash)).fetchone()
                if existing:
                    return "brand_attachment", existing["attachment_id"], \
                        {"attachment_id": existing["attachment_id"], "duplicate": True}
                attachment_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO brand_attachments(attachment_id,agreement_id,version_no,kind,filename,"
                    "content_hash,uploaded_by,uploaded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (attachment_id, agreement_id, version["version_no"], kind, filename,
                     content_hash, actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="brand_attachment.uploaded",
                             resource_type="brand_attachment", resource_id=attachment_id,
                             detail={"agreement_id": agreement_id, "version_no": version["version_no"],
                                     "kind": kind, "content_hash": content_hash}, occurred_at=now)
                return "brand_attachment", attachment_id, {"attachment_id": attachment_id, "duplicate": False}

            return self._idempotent(connection, request_id=request_id,
                                    action="brand_upload_attachment", payload=payload, create=create)

    def remove_plan_item(self, *, request_id: str, actor_id: str, agreement_id: str,
                         kind: str, item_id: str) -> Any:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "kind": kind, "item_id": item_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._brand_actor(connection, actor_id)
            self._require(actor, *LEGAL_ROLES)
            _, version = self._draft_version(connection, agreement_id)
            no = version["version_no"]
            if kind not in {"session", "entitlement", "fee", "material"}:
                raise ValidationError("kind 必须是 session、entitlement、fee 或 material")
            table = {
                "session": "brand_sessions",
                "entitlement": "brand_entitlements",
                "fee": "brand_fee_commitments",
                "material": "brand_material_deliveries",
            }[kind]
            id_column = {"session": "session_id", "entitlement": "entitlement_id",
                         "fee": "fee_id", "material": "delivery_id"}[kind]

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    f"SELECT 1 FROM {table} WHERE agreement_id=? AND version_no=? AND {id_column}=?",
                    (agreement_id, no, item_id)).fetchone()
                if existing is None:
                    raise NotFoundError("计划条目不存在")
                if kind == "session":
                    referenced = connection.execute(
                        "SELECT entitlement_id FROM brand_entitlements WHERE agreement_id=? AND version_no=? "
                        "AND session_id=?", (agreement_id, no, item_id)).fetchone()
                    if referenced:
                        raise ConflictError(f"场次仍被权益 {referenced['entitlement_id']} 引用，请先移除该权益")
                if kind == "entitlement":
                    connection.execute(
                        "DELETE FROM brand_fee_commitments WHERE agreement_id=? AND version_no=? AND entitlement_id=?",
                        (agreement_id, no, item_id))
                    connection.execute(
                        "UPDATE brand_material_deliveries SET entitlement_id=NULL WHERE agreement_id=? "
                        "AND version_no=? AND entitlement_id=?", (agreement_id, no, item_id))
                connection.execute(
                    f"DELETE FROM {table} WHERE agreement_id=? AND version_no=? AND {id_column}=?",
                    (agreement_id, no, item_id))
                append_event(connection, actor_id=actor_id, action="brand_plan_item.removed",
                             resource_type=table, resource_id=f"{agreement_id}/{item_id}",
                             detail={"kind": kind, "version_no": no}, occurred_at=self._now())
                return table, item_id, {"kind": kind, "item_id": item_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="brand_remove_plan_item", payload=payload, create=create)

    # ------------------------------------------------------------------ 冲突识别

    def check_conflicts(self, actor_id: str, agreement_id: str, version_no: int | None = None) -> list[dict]:
        with self.database.transaction() as connection:
            self._brand_actor(connection, actor_id)
            if version_no is None:
                agreement = self._agreement_row(connection, agreement_id)
                version_no = agreement["current_version"]
            self._version_row(connection, agreement_id, int(version_no))
            return [conflict.to_dict() for conflict in
                    self._scan_conflicts(connection, agreement_id, int(version_no))]

    def _scan_conflicts(self, connection, agreement_id: str, version_no: int) -> list[BrandConflict]:
        candidates = connection.execute(
            "SELECT * FROM brand_entitlements WHERE agreement_id=? AND version_no=?",
            (agreement_id, version_no)).fetchall()
        window_cache: dict[tuple[str, int, str], tuple[datetime, datetime]] = {}

        def window_of(row) -> tuple[datetime, datetime]:
            key = (row["agreement_id"], row["version_no"], row["entitlement_id"])
            if key not in window_cache:
                window_cache[key] = self._window(connection, row)
            return window_cache[key]

        conflicts: list[BrandConflict] = []

        def pair_conflict(left, right, left_agreement: str, right_agreement: str) -> None:
            cross_agreement = left_agreement != right_agreement
            if not region_overlaps(left["region_code"], right["region_code"]):
                return
            lw = window_of(left)
            rw = window_of(right)
            if not overlaps(lw[0], lw[1], rw[0], rw[1]):
                return
            # 排他品类冲突只在不同合作方（不同协议）之间判定；同一协议内排他与供货共存是正常组合
            if cross_agreement and left["category_code"] and \
                    left["category_code"] == right["category_code"] and \
                    (left["exclusive"] or right["exclusive"]):
                conflicts.append(BrandConflict(
                    "exclusive_category",
                    f"品类 {left['category_code']} 的排他范围在时间与区域上重叠",
                    left_agreement, left["entitlement_id"], right_agreement, right["entitlement_id"]))
            # 露出位/媒体位/活动分区无论是否跨协议，同一物理位置不得重复承诺
            if left["kind"] == "exposure_position" and right["kind"] == "exposure_position" and \
                    left["placement_code"] and left["placement_code"] == right["placement_code"]:
                conflicts.append(BrandConflict(
                    "exposure_position",
                    f"露出位置 {left['placement_code']} 在同一时间窗口被重复承诺",
                    left_agreement, left["entitlement_id"], right_agreement, right["entitlement_id"]))
            if left["kind"] == "media_asset" and right["kind"] == "media_asset" and \
                    left["asset_code"] and left["asset_code"] == right["asset_code"]:
                conflicts.append(BrandConflict(
                    "media_slot",
                    f"媒体资产 {left['asset_code']} 的投放窗口重叠",
                    left_agreement, left["entitlement_id"], right_agreement, right["entitlement_id"]))
            if left["kind"] == "activity_window" and right["kind"] == "activity_window" and \
                    left["zone_code"] and left["zone_code"] == right["zone_code"]:
                conflicts.append(BrandConflict(
                    "activity_zone",
                    f"现场活动分区 {left['zone_code']} 的活动窗口重叠",
                    left_agreement, left["entitlement_id"], right_agreement, right["entitlement_id"]))

        # 同一版本内部重复承诺
        for i, left in enumerate(candidates):
            for right in candidates[i + 1:]:
                pair_conflict(left, right, agreement_id, agreement_id)

        # 与其他合作方已签署生效版本比较
        others = connection.execute(
            "SELECT e.* FROM brand_entitlements e JOIN brand_agreements a ON a.agreement_id=e.agreement_id "
            "JOIN brand_agreement_versions v ON v.agreement_id=e.agreement_id AND v.version_no=e.version_no "
            "WHERE a.status='active' AND v.status='signed' AND e.agreement_id<>?",
            (agreement_id,)).fetchall()
        for left in candidates:
            for right in others:
                pair_conflict(left, right, agreement_id, right["agreement_id"])
        # 去重（同四元组可能由多条规则命中不同 kind，保留 kind 维度的去重）
        seen = set()
        unique: list[BrandConflict] = []
        for conflict in conflicts:
            key = (conflict.kind, conflict.entitlement_id, conflict.other_entitlement_id)
            if key not in seen:
                seen.add(key)
                unique.append(conflict)
        return unique

    # ------------------------------------------------------------------ 物料状态

    def _advance_material(self, connection, agreement_id: str, delivery_id: str, expected: str):
        version_no = self._signed_version_no(connection, agreement_id)
        row = connection.execute(
            "SELECT * FROM brand_material_deliveries WHERE agreement_id=? AND version_no=? AND delivery_id=?",
            (agreement_id, version_no, delivery_id)).fetchone()
        if row is None:
            raise NotFoundError("物料不存在")
        if row["status"] != expected:
            raise ConflictError(f"物料当前状态为 {row['status']}，不能执行该动作")
        return row, version_no

    def mark_material_delivered(self, *, request_id: str, actor_id: str, agreement_id: str,
                                delivery_id: str) -> Any:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "delivery_id": delivery_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._brand_actor(connection, actor_id)
            self._require(actor, *MARKET_ROLES)
            row, version_no = self._advance_material(connection, agreement_id, delivery_id, "pending")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                connection.execute(
                    "UPDATE brand_material_deliveries SET status='delivered',delivered_at=? "
                    "WHERE agreement_id=? AND version_no=? AND delivery_id=?",
                    (now, agreement_id, version_no, delivery_id))
                append_event(connection, actor_id=actor_id, action="brand_material.delivered",
                             resource_type="brand_material", resource_id=f"{agreement_id}/{delivery_id}",
                             detail={"title": row["title"]}, occurred_at=now)
                return "brand_material", delivery_id, {"delivery_id": delivery_id, "status": "delivered"}

            return self._idempotent(connection, request_id=request_id,
                                    action="brand_material_delivered", payload=payload, create=create)

    def confirm_material(self, *, request_id: str, actor_id: str, agreement_id: str,
                         delivery_id: str) -> Any:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "delivery_id": delivery_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._brand_actor(connection, actor_id)
            self._require(actor, *MARKET_ROLES)
            row, version_no = self._advance_material(connection, agreement_id, delivery_id, "delivered")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                connection.execute(
                    "UPDATE brand_material_deliveries SET status='confirmed',confirmed_by=?,confirmed_at=? "
                    "WHERE agreement_id=? AND version_no=? AND delivery_id=?",
                    (actor_id, now, agreement_id, version_no, delivery_id))
                append_event(connection, actor_id=actor_id, action="brand_material.confirmed",
                             resource_type="brand_material", resource_id=f"{agreement_id}/{delivery_id}",
                             detail={"title": row["title"]}, occurred_at=now)
                return "brand_material", delivery_id, {"delivery_id": delivery_id, "status": "confirmed"}

            return self._idempotent(connection, request_id=request_id,
                                    action="brand_material_confirmed", payload=payload, create=create)

    # ------------------------------------------------------------------ 证据与验收

    def upload_evidence(self, *, request_id: str, actor_id: str, agreement_id: str, entitlement_id: str,
                        filename: str, content_hash: str, media_type: str, size_bytes: int) -> Any:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "entitlement_id": entitlement_id,
                   "filename": filename, "content_hash": content_hash, "media_type": media_type,
                   "size_bytes": size_bytes}
        with self.database.transaction(immediate=True) as connection:
            actor = self._brand_actor(connection, actor_id)
            self._require(actor, *MARKET_ROLES)
            version_no = self._signed_version_no(connection, agreement_id)
            self._entitlement_row(connection, agreement_id, version_no, entitlement_id)
            filename = self._text(filename, "filename", 240)
            content_hash = self._text(content_hash, "content_hash", 128)
            media_type = self._text(media_type, "media_type", 80)
            if not isinstance(size_bytes, int) or size_bytes < 0:
                raise ValidationError("size_bytes 必须是非负整数")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                existing = connection.execute(
                    "SELECT evidence_id FROM brand_evidence WHERE agreement_id=? AND version_no=? "
                    "AND entitlement_id=? AND content_hash=?",
                    (agreement_id, version_no, entitlement_id, content_hash)).fetchone()
                if existing:
                    # 重复凭证：返回既有证据，绝不产生第二条验收
                    return "brand_evidence", existing["evidence_id"], \
                        {"evidence_id": existing["evidence_id"], "duplicate": True}
                evidence_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO brand_evidence(evidence_id,agreement_id,version_no,entitlement_id,"
                    "content_hash,filename,media_type,size_bytes,uploaded_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (evidence_id, agreement_id, version_no, entitlement_id, content_hash, filename,
                     media_type, size_bytes, actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="brand_evidence.uploaded",
                             resource_type="brand_evidence", resource_id=evidence_id,
                             detail={"agreement_id": agreement_id, "entitlement_id": entitlement_id,
                                     "content_hash": content_hash}, occurred_at=now)
                return "brand_evidence", evidence_id, {"evidence_id": evidence_id, "duplicate": False}

            return self._idempotent(connection, request_id=request_id,
                                    action="brand_upload_evidence", payload=payload, create=create)

    def decide_acceptance(self, *, request_id: str, actor_id: str, evidence_id: str, decision: str,
                          accepted_ratio: float | None = None, accepted_quantity: float | None = None,
                          note: str = "") -> Any:
        payload = {"actor_id": actor_id, "evidence_id": evidence_id, "decision": decision,
                   "accepted_ratio": accepted_ratio, "accepted_quantity": accepted_quantity, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._brand_actor(connection, actor_id)
            self._require(actor, *MARKET_ROLES)
            evidence = connection.execute(
                "SELECT * FROM brand_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
            if evidence is None:
                raise NotFoundError("证据不存在")
            agreement_id = evidence["agreement_id"]
            version_no = evidence["version_no"]
            entitlement_id = evidence["entitlement_id"]
            if decision not in {"accepted", "partial", "returned", "disputed", "rejected"}:
                raise ValidationError("decision 不在允许范围内")
            ratio = self._acceptance_ratio(connection, decision, evidence, accepted_ratio, accepted_quantity)
            note = str(note or "")[:1000]
            if decision == "disputed" and not note:
                raise ValidationError("提出争议必须填写原因")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                prior = connection.execute(
                    "SELECT acceptance_id FROM brand_acceptances WHERE evidence_id=?", (evidence_id,)
                ).fetchone()
                if prior:
                    raise ConflictError("该凭证已经存在验收结论，重复上传或重复验收不会产生第二次验收")
                acceptance_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO brand_acceptances(acceptance_id,evidence_id,agreement_id,version_no,"
                    "entitlement_id,decision,accepted_quantity,accepted_ratio,note,decided_by,decided_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (acceptance_id, evidence_id, agreement_id, version_no, entitlement_id, decision,
                     accepted_quantity if decision == "partial" else None, ratio, note, actor_id, now),
                )
                detail = {"decision": decision, "accepted_ratio": ratio,
                          "entitlement_id": entitlement_id}
                if decision == "disputed":
                    dispute_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO brand_disputes(dispute_id,acceptance_id,agreement_id,version_no,"
                        "entitlement_id,evidence_id,reason,status,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?, 'open',?,?)",
                        (dispute_id, acceptance_id, agreement_id, version_no, entitlement_id, evidence_id,
                         note, actor_id, now),
                    )
                    detail["dispute_id"] = dispute_id
                append_event(connection, actor_id=actor_id, action="brand_acceptance.decided",
                             resource_type="brand_acceptance", resource_id=acceptance_id,
                             detail=detail, occurred_at=now)
                response = {"acceptance_id": acceptance_id, "decision": decision,
                            "accepted_ratio": ratio}
                if decision == "disputed":
                    response["dispute_id"] = detail["dispute_id"]
                    response["fee_frozen"] = True
                return "brand_acceptance", acceptance_id, response

            return self._idempotent(connection, request_id=request_id,
                                    action="brand_decide_acceptance", payload=payload, create=create)

    def _acceptance_ratio(self, connection, decision: str, evidence, ratio: float | None,
                          quantity: float | None) -> float:
        if decision == "accepted":
            return 1.0
        if decision in {"returned", "rejected", "disputed"}:
            return 0.0
        # partial：可按比例或按数量折算
        if ratio is not None and quantity is not None:
            raise ValidationError("部分验收只能提供 accepted_ratio 或 accepted_quantity 之一")
        if ratio is not None:
            if not 0 < ratio < 1:
                raise ValidationError("accepted_ratio 必须位于 0 与 1 之间")
            return float(ratio)
        if quantity is not None:
            if quantity <= 0:
                raise ValidationError("accepted_quantity 必须为正数")
            ent = self._entitlement_row(connection, evidence["agreement_id"], evidence["version_no"],
                                        evidence["entitlement_id"])
            if not ent["quantity"]:
                raise ValidationError("权益未约定总数量，不能按数量部分验收，请改用 accepted_ratio")
            value = quantity / ent["quantity"]
            if not 0 < value < 1:
                raise ValidationError("部分验收比例必须严格位于 0 与 1 之间")
            return value
        raise ValidationError("部分验收必须提供 accepted_ratio 或 accepted_quantity")

    def resolve_dispute(self, *, request_id: str, actor_id: str, dispute_id: str, outcome: str,
                        resolution_note: str) -> Any:
        payload = {"actor_id": actor_id, "dispute_id": dispute_id, "outcome": outcome,
                   "resolution_note": resolution_note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._brand_actor(connection, actor_id)
            self._require(actor, *LEGAL_ROLES)
            dispute = connection.execute(
                "SELECT * FROM brand_disputes WHERE dispute_id=?", (dispute_id,)).fetchone()
            if dispute is None:
                raise NotFoundError("争议不存在")
            if dispute["status"] != "open":
                raise ConflictError("争议已经裁决")
            if outcome not in {"upheld", "rejected"}:
                raise ValidationError("outcome 必须是 upheld（权益未成立）或 rejected（驳回争议）")
            resolution_note = self._text(resolution_note, "resolution_note", 1000)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                connection.execute(
                    "UPDATE brand_disputes SET status=?,resolution_note=?,resolved_by=?,resolved_at=? "
                    "WHERE dispute_id=?",
                    (outcome, resolution_note, actor_id, now, dispute_id))
                # upheld：维持不验收（费用不再冻结但无可付比例）；rejected：争议驳回，按全额验收恢复结算
                new_decision = "rejected" if outcome == "upheld" else "accepted"
                new_ratio = 0.0 if outcome == "upheld" else 1.0
                connection.execute(
                    "UPDATE brand_acceptances SET decision=?,accepted_ratio=? WHERE acceptance_id=?",
                    (new_decision, new_ratio, dispute["acceptance_id"]))
                append_event(connection, actor_id=actor_id, action="brand_dispute.resolved",
                             resource_type="brand_dispute", resource_id=dispute_id,
                             detail={"outcome": outcome, "entitlement_id": dispute["entitlement_id"],
                                     "acceptance_id": dispute["acceptance_id"]}, occurred_at=now)
                return "brand_dispute", dispute_id, {"dispute_id": dispute_id, "status": outcome}

            return self._idempotent(connection, request_id=request_id,
                                    action="brand_resolve_dispute", payload=payload, create=create)

    # ------------------------------------------------------------------ 结算

    def _entitlement_fulfillment(self, connection, agreement_id: str, version_no: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        rows = connection.execute(
            "SELECT a.entitlement_id, a.decision, a.accepted_ratio, d.status AS dispute_status "
            "FROM brand_acceptances a LEFT JOIN brand_disputes d ON d.acceptance_id=a.acceptance_id "
            "WHERE a.agreement_id=? AND a.version_no=?", (agreement_id, version_no)).fetchall()
        for row in rows:
            item = result.setdefault(row["entitlement_id"], {"ratio": 0.0, "frozen": False})
            if row["dispute_status"] == "open":
                item["frozen"] = True
            if row["decision"] in {"accepted", "partial"}:
                item["ratio"] = min(1.0, item["ratio"] + row["accepted_ratio"])
        return result

    def create_payment(self, *, request_id: str, actor_id: str, agreement_id: str, payment_id: str,
                       currency: str, amount_minor: int, allocations: list[dict[str, Any]],
                       note: str = "") -> Any:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "payment_id": payment_id,
                   "currency": currency, "amount_minor": amount_minor, "allocations": allocations,
                   "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._brand_actor(connection, actor_id)
            self._require(actor, *FINANCE_ROLES)
            version_no = self._signed_version_no(connection, agreement_id)
            payment_id = self._identifier(payment_id, "payment_id")
            currency = self._text(currency, "currency", 8).upper()
            if not isinstance(amount_minor, int) or amount_minor < 0:
                raise ValidationError("amount_minor 必须是非负整数")
            if not isinstance(allocations, list) or not allocations:
                raise ValidationError("allocations 必须是非空数组，说明每笔钱对应的费用、验收与证据")
            note = str(note or "")[:500]
            fulfillment = self._entitlement_fulfillment(connection, agreement_id, version_no)
            total = 0
            normalized: list[dict[str, Any]] = []
            fee_paid: dict[str, int] = {}
            for entry in allocations:
                fee_id = self._identifier(str(entry.get("fee_id", "")), "fee_id")
                amount = entry.get("amount_minor")
                if not isinstance(amount, int) or amount <= 0:
                    raise ValidationError("分配金额必须是正整数")
                fee = connection.execute(
                    "SELECT * FROM brand_fee_commitments WHERE agreement_id=? AND version_no=? AND fee_id=?",
                    (agreement_id, version_no, fee_id)).fetchone()
                if fee is None:
                    raise NotFoundError(f"费用 {fee_id} 不存在")
                if fee["currency"] != currency:
                    raise ValidationError(f"费用 {fee_id} 的币种与付款不一致")
                progress = fulfillment.get(fee["entitlement_id"], {"ratio": 0.0, "frozen": False})
                if progress["frozen"]:
                    raise ConflictError(f"权益 {fee['entitlement_id']} 存在未裁决争议，相关费用已冻结")
                eligible = int(fee["amount_minor"] * progress["ratio"] + 1e-9)
                already = connection.execute(
                    "SELECT COALESCE(SUM(amount_minor),0) AS total FROM brand_payment_allocations "
                    "WHERE agreement_id=? AND version_no=? AND fee_id=?",
                    (agreement_id, version_no, fee_id)).fetchone()["total"]
                fee_paid[fee_id] = already
                if already + amount > eligible:
                    raise ConflictError(
                        f"费用 {fee_id} 当前有效履约比例 {progress['ratio']:.2f}，可付 {eligible}，"
                        f"已付 {already}，本次申请 {amount} 超出可付额度")
                acceptance_ids = entry.get("acceptance_ids") or []
                evidence_ids = entry.get("evidence_ids") or []
                if not isinstance(acceptance_ids, list) or not acceptance_ids:
                    raise ValidationError(f"费用 {fee_id} 的分配必须引用至少一条有效验收")
                if not isinstance(evidence_ids, list) or not evidence_ids:
                    raise ValidationError(f"费用 {fee_id} 的分配必须引用支撑证据")
                for acceptance_id in acceptance_ids:
                    acc = connection.execute(
                        "SELECT * FROM brand_acceptances WHERE acceptance_id=? AND agreement_id=? "
                        "AND version_no=? AND entitlement_id=?",
                        (acceptance_id, agreement_id, version_no, fee["entitlement_id"])).fetchone()
                    if acc is None:
                        raise NotFoundError(f"验收 {acceptance_id} 不属于费用 {fee_id} 对应的权益")
                    if acc["decision"] not in {"accepted", "partial"}:
                        raise ConflictError(f"验收 {acceptance_id} 不是有效验收，不能用于付款")
                for evidence_id in evidence_ids:
                    ev = connection.execute(
                        "SELECT * FROM brand_evidence WHERE evidence_id=? AND agreement_id=? "
                        "AND version_no=? AND entitlement_id=?",
                        (evidence_id, agreement_id, version_no, fee["entitlement_id"])).fetchone()
                    if ev is None:
                        raise NotFoundError(f"证据 {evidence_id} 不属于费用 {fee_id} 对应的权益")
                    linked = connection.execute(
                        "SELECT 1 FROM brand_acceptances WHERE evidence_id=? AND acceptance_id IN (%s)"
                        % ",".join("?" for _ in acceptance_ids),
                        [evidence_id, *acceptance_ids]).fetchone()
                    if linked is None:
                        raise ValidationError(f"证据 {evidence_id} 没有被所引用的验收覆盖，不能作为付款依据")
                total += amount
                normalized.append({"fee_id": fee_id, "amount_minor": amount,
                                   "acceptance_ids": acceptance_ids, "evidence_ids": evidence_ids})
            if total != amount_minor:
                raise ValidationError(
                    f"分配金额合计 {total} 必须与付款总额 {amount_minor} 完全相等，确保每笔钱都有对应解释")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                try:
                    connection.execute(
                        "INSERT INTO brand_payments(payment_id,agreement_id,currency,amount_minor,note,"
                        "paid_by,paid_at) VALUES(?,?,?,?,?,?,?)",
                        (payment_id, agreement_id, currency, amount_minor, note, actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("付款编号已经存在") from exc
                allocation_ids = []
                for entry in normalized:
                    allocation_id = uuid.uuid4().hex
                    allocation_ids.append(allocation_id)
                    connection.execute(
                        "INSERT INTO brand_payment_allocations(allocation_id,payment_id,agreement_id,"
                        "version_no,fee_id,entitlement_id,amount_minor,acceptance_ids_json,evidence_ids_json)"
                        " SELECT ?,?,?,?, ?,fee.entitlement_id, ?,?,? FROM brand_fee_commitments fee "
                        "WHERE fee.agreement_id=? AND fee.version_no=? AND fee.fee_id=?",
                        (allocation_id, payment_id, agreement_id, version_no, entry["fee_id"],
                         entry["amount_minor"], self._json(entry["acceptance_ids"]),
                         self._json(entry["evidence_ids"]), agreement_id, version_no, entry["fee_id"]))
                append_event(connection, actor_id=actor_id, action="brand_payment.created",
                             resource_type="brand_payment", resource_id=payment_id,
                             detail={"agreement_id": agreement_id, "currency": currency,
                                     "amount_minor": amount_minor, "allocations": len(normalized)},
                             occurred_at=now)
                return "brand_payment", payment_id, {"payment_id": payment_id,
                                                      "allocated_minor": total,
                                                      "allocation_count": len(normalized)}

            return self._idempotent(connection, request_id=request_id,
                                    action="brand_create_payment", payload=payload, create=create)

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    # ------------------------------------------------------------------ 查询视图

    def _can_see_amounts(self, role: str) -> bool:
        return role in READ_ALL_ROLES or role == "finance"

    def get_agreement_detail(self, actor_id: str, agreement_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            actor = self._brand_actor(connection, actor_id)
            agreement = self._agreement_row(connection, agreement_id)
            show_amounts = self._can_see_amounts(actor.role)
            versions = [dict(row) for row in connection.execute(
                "SELECT * FROM brand_agreement_versions WHERE agreement_id=? ORDER BY version_no",
                (agreement_id,))]
            current_no = agreement["current_version"]
            sessions = [dict(row) for row in connection.execute(
                "SELECT * FROM brand_sessions WHERE agreement_id=? AND version_no=? ORDER BY starts_at",
                (agreement_id, current_no))]
            entitlements = [dict(row) for row in connection.execute(
                "SELECT * FROM brand_entitlements WHERE agreement_id=? AND version_no=? ORDER BY entitlement_id",
                (agreement_id, current_no))]
            for ent in entitlements:
                ent["exclusive"] = bool(ent["exclusive"])
            fees = []
            for row in connection.execute(
                "SELECT * FROM brand_fee_commitments WHERE agreement_id=? AND version_no=? ORDER BY fee_id",
                    (agreement_id, current_no)):
                item = dict(row)
                if not show_amounts:
                    item["amount_minor"] = None
                fees.append(item)
            materials = [dict(row) for row in connection.execute(
                "SELECT * FROM brand_material_deliveries WHERE agreement_id=? AND version_no=? "
                "ORDER BY delivery_id", (agreement_id, current_no))]
            attachments = [dict(row) for row in connection.execute(
                "SELECT attachment_id,version_no,kind,filename,content_hash,uploaded_by,uploaded_at "
                "FROM brand_attachments WHERE agreement_id=? ORDER BY version_no, uploaded_at",
                (agreement_id,))]
            disputes = [dict(row) for row in connection.execute(
                "SELECT * FROM brand_disputes WHERE agreement_id=? ORDER BY created_at", (agreement_id,))]
            return {
                "agreement_id": agreement["agreement_id"],
                "partner_id": agreement["partner_id"],
                "partner_name": agreement["partner_name"],
                "status": agreement["status"],
                "current_version": current_no,
                "renewal_of_agreement_id": agreement["renewal_of_agreement_id"],
                "created_by": agreement["created_by"],
                "created_at": agreement["created_at"],
                "versions": versions,
                "sessions": sessions,
                "entitlements": entitlements,
                "fees": fees,
                "materials": materials,
                "attachments": attachments,
                "disputes": disputes,
                "viewer_role": actor.role,
                "amounts_visible": show_amounts,
            }

    def list_agreements(self, actor_id: str) -> list[dict[str, Any]]:
        with self.database.transaction() as connection:
            actor = self._brand_actor(connection, actor_id)
            rows = connection.execute(
                "SELECT agreement_id,partner_id,partner_name,current_version,status,"
                "renewal_of_agreement_id,created_at FROM brand_agreements ORDER BY created_at").fetchall()
            return [dict(row) for row in rows]

    def get_entitlement_tracking(self, actor_id: str, agreement_id: str,
                                 entitlement_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            actor = self._brand_actor(connection, actor_id)
            version_no = self._signed_version_no(connection, agreement_id)
            entitlement = self._entitlement_row(connection, agreement_id, version_no, entitlement_id)
            evidence = [dict(row) for row in connection.execute(
                "SELECT * FROM brand_evidence WHERE agreement_id=? AND version_no=? AND entitlement_id=? "
                "ORDER BY created_at", (agreement_id, version_no, entitlement_id))]
            acceptances = [dict(row) for row in connection.execute(
                "SELECT * FROM brand_acceptances WHERE agreement_id=? AND version_no=? AND entitlement_id=? "
                "ORDER BY decided_at", (agreement_id, version_no, entitlement_id))]
            disputes = [dict(row) for row in connection.execute(
                "SELECT * FROM brand_disputes WHERE agreement_id=? AND version_no=? AND entitlement_id=? "
                "ORDER BY created_at", (agreement_id, version_no, entitlement_id))]
            item = dict(entitlement)
            item["exclusive"] = bool(item["exclusive"])
            return {"entitlement": item, "evidence": evidence, "acceptances": acceptances,
                    "disputes": disputes, "viewer_role": actor.role}

    def settlement_report(self, actor_id: str, agreement_id: str) -> dict[str, Any]:
        """按费用行说明履约比例、冻结状态、可付与已付金额。"""

        with self.database.transaction() as connection:
            actor = self._brand_actor(connection, actor_id)
            if actor.role not in READ_ALL_ROLES and actor.role not in FINANCE_ROLES:
                raise PermissionDenied("结算明细仅法务、财务和审计可查看")
            version_no = self._signed_version_no(connection, agreement_id)
            fulfillment = self._entitlement_fulfillment(connection, agreement_id, version_no)
            fees = []
            for fee in connection.execute(
                "SELECT * FROM brand_fee_commitments WHERE agreement_id=? AND version_no=? ORDER BY fee_id",
                    (agreement_id, version_no)):
                progress = fulfillment.get(fee["entitlement_id"], {"ratio": 0.0, "frozen": False})
                ratio = 0.0 if progress["frozen"] else progress["ratio"]
                eligible = int(fee["amount_minor"] * ratio + 1e-9)
                paid = connection.execute(
                    "SELECT COALESCE(SUM(amount_minor),0) AS total FROM brand_payment_allocations "
                    "WHERE agreement_id=? AND version_no=? AND fee_id=?",
                    (agreement_id, version_no, fee["fee_id"])).fetchone()["total"]
                fees.append({
                    "fee_id": fee["fee_id"],
                    "entitlement_id": fee["entitlement_id"],
                    "title": fee["title"],
                    "currency": fee["currency"],
                    "amount_minor": fee["amount_minor"],
                    "accepted_ratio": round(progress["ratio"], 6),
                    "frozen": bool(progress["frozen"]),
                    "eligible_minor": eligible,
                    "paid_minor": paid,
                    "payable_minor": max(0, eligible - paid),
                })
            totals: dict[str, dict[str, int]] = {}
            for fee in fees:
                bucket = totals.setdefault(fee["currency"], {"committed": 0, "eligible": 0, "paid": 0,
                                                             "frozen": 0})
                bucket["committed"] += fee["amount_minor"]
                bucket["eligible"] += fee["eligible_minor"]
                bucket["paid"] += fee["paid_minor"]
                if fee["frozen"]:
                    bucket["frozen"] += fee["amount_minor"]
            return {"agreement_id": agreement_id, "version_no": version_no, "fees": fees,
                    "totals": totals, "viewer_role": actor.role}

    def explain_payment(self, actor_id: str, payment_id: str) -> dict[str, Any]:
        """解释一笔付款对应哪些费用、有效权益、验收结论与证据。"""

        with self.database.transaction() as connection:
            actor = self._brand_actor(connection, actor_id)
            payment = connection.execute(
                "SELECT * FROM brand_payments WHERE payment_id=?", (payment_id,)).fetchone()
            if payment is None:
                raise NotFoundError("付款不存在")
            show_amounts = self._can_see_amounts(actor.role)
            allocations = []
            for alloc in connection.execute(
                "SELECT * FROM brand_payment_allocations WHERE payment_id=? ORDER BY allocation_id",
                    (payment_id,)):
                acceptance_ids = json.loads(alloc["acceptance_ids_json"])
                evidence_ids = json.loads(alloc["evidence_ids_json"])
                entitlement = connection.execute(
                    "SELECT * FROM brand_entitlements WHERE agreement_id=? AND version_no=? AND entitlement_id=?",
                    (alloc["agreement_id"], alloc["version_no"], alloc["entitlement_id"])).fetchone()
                fee = connection.execute(
                    "SELECT * FROM brand_fee_commitments WHERE agreement_id=? AND version_no=? AND fee_id=?",
                    (alloc["agreement_id"], alloc["version_no"], alloc["fee_id"])).fetchone()
                acceptances = []
                for acceptance_id in acceptance_ids:
                    row = connection.execute(
                        "SELECT * FROM brand_acceptances WHERE acceptance_id=?", (acceptance_id,)).fetchone()
                    acceptances.append(dict(row) if row else {"acceptance_id": acceptance_id, "missing": True})
                evidences = []
                for evidence_id in evidence_ids:
                    row = connection.execute(
                        "SELECT evidence_id,filename,media_type,content_hash,uploaded_by,created_at "
                        "FROM brand_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
                    evidences.append(dict(row) if row else {"evidence_id": evidence_id, "missing": True})
                item = {
                    "allocation_id": alloc["allocation_id"],
                    "fee_id": alloc["fee_id"],
                    "fee_title": fee["title"] if fee else None,
                    "entitlement_id": alloc["entitlement_id"],
                    "entitlement": dict(entitlement) if entitlement else None,
                    "acceptances": acceptances,
                    "evidence": evidences,
                }
                if item["entitlement"] is not None:
                    item["entitlement"]["exclusive"] = bool(item["entitlement"]["exclusive"])
                if show_amounts:
                    item["amount_minor"] = alloc["amount_minor"]
                allocations.append(item)
            result = {
                "payment_id": payment["payment_id"],
                "agreement_id": payment["agreement_id"],
                "currency": payment["currency"],
                "note": payment["note"],
                "paid_by": payment["paid_by"],
                "paid_at": payment["paid_at"],
                "allocations": allocations,
                "viewer_role": actor.role,
                "amounts_visible": show_amounts,
            }
            if show_amounts:
                result["amount_minor"] = payment["amount_minor"]
            return result
