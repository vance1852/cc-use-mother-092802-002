"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
-- 品牌权益履约：协议与版本
CREATE TABLE IF NOT EXISTS brand_agreements (
    agreement_id TEXT PRIMARY KEY,
    partner_id TEXT NOT NULL,
    partner_name TEXT NOT NULL,
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    status TEXT NOT NULL CHECK(status IN ('draft', 'active')),
    renewal_of_agreement_id TEXT REFERENCES brand_agreements(agreement_id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brand_agreement_versions (
    agreement_id TEXT NOT NULL REFERENCES brand_agreements(agreement_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    status TEXT NOT NULL CHECK(status IN ('draft', 'signed', 'superseded')),
    change_note TEXT NOT NULL DEFAULT '',
    signed_by TEXT,
    signed_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (agreement_id, version_no)
);
CREATE TABLE IF NOT EXISTS brand_attachments (
    attachment_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    kind TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    uploaded_by TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    UNIQUE(agreement_id, version_no, content_hash)
);
-- 品牌权益履约：场次与区域
CREATE TABLE IF NOT EXISTS brand_sessions (
    agreement_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('racing', 'football', 'music')),
    name TEXT NOT NULL,
    region_code TEXT NOT NULL,
    venue TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    PRIMARY KEY (agreement_id, version_no, session_id)
);
-- 品牌权益：露出位置、媒体资产、现场活动窗口、供货与排他范围
CREATE TABLE IF NOT EXISTS brand_entitlements (
    agreement_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    entitlement_id TEXT NOT NULL,
    session_id TEXT,
    region_code TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('exposure_position', 'media_asset', 'activity_window', 'supply')),
    placement_code TEXT,
    asset_code TEXT,
    zone_code TEXT,
    category_code TEXT,
    exclusive INTEGER NOT NULL DEFAULT 0 CHECK(exclusive IN (0, 1)),
    starts_at TEXT,
    ends_at TEXT,
    quantity REAL,
    unit TEXT,
    title TEXT NOT NULL,
    PRIMARY KEY (agreement_id, version_no, entitlement_id)
);
-- 费用承诺按权益拆分，保证争议可以只冻结相关费用
CREATE TABLE IF NOT EXISTS brand_fee_commitments (
    agreement_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    fee_id TEXT NOT NULL,
    entitlement_id TEXT NOT NULL,
    title TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    currency TEXT NOT NULL,
    due_at TEXT,
    PRIMARY KEY (agreement_id, version_no, fee_id)
);
-- 物料交付
CREATE TABLE IF NOT EXISTS brand_material_deliveries (
    agreement_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    delivery_id TEXT NOT NULL,
    session_id TEXT,
    entitlement_id TEXT,
    title TEXT NOT NULL,
    quantity REAL,
    unit TEXT,
    due_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending', 'delivered', 'confirmed')),
    delivered_at TEXT,
    confirmed_by TEXT,
    confirmed_at TEXT,
    PRIMARY KEY (agreement_id, version_no, delivery_id)
);
-- 现场证据：同一权益下内容哈希相同的凭证只能存在一份
CREATE TABLE IF NOT EXISTS brand_evidence (
    evidence_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    entitlement_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    uploaded_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(agreement_id, version_no, entitlement_id, content_hash)
);
-- 验收结论：每份证据至多一条结论；部分验收记录数量或比例
CREATE TABLE IF NOT EXISTS brand_acceptances (
    acceptance_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL UNIQUE REFERENCES brand_evidence(evidence_id),
    agreement_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    entitlement_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('accepted', 'partial', 'returned', 'disputed', 'rejected')),
    accepted_quantity REAL,
    accepted_ratio REAL NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_brand_acceptances_ent
    ON brand_acceptances(agreement_id, version_no, entitlement_id);
-- 争议：open 期间冻结相关费用；upheld 表示权益未交付，rejected 表示驳回争议
CREATE TABLE IF NOT EXISTS brand_disputes (
    dispute_id TEXT PRIMARY KEY,
    acceptance_id TEXT NOT NULL REFERENCES brand_acceptances(acceptance_id),
    agreement_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    entitlement_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'upheld', 'rejected')),
    resolution_note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_by TEXT,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS brand_disputes_ent_idx
    ON brand_disputes(agreement_id, version_no, entitlement_id);
-- 付款与分配快照：解释每笔钱对应哪些有效权益、验收和证据
CREATE TABLE IF NOT EXISTS brand_payments (
    payment_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES brand_agreements(agreement_id),
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK(amount_minor >= 0),
    note TEXT NOT NULL DEFAULT '',
    paid_by TEXT NOT NULL,
    paid_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brand_payment_allocations (
    allocation_id TEXT PRIMARY KEY,
    payment_id TEXT NOT NULL REFERENCES brand_payments(payment_id),
    agreement_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    fee_id TEXT NOT NULL,
    entitlement_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    acceptance_ids_json TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS brand_allocations_fee_idx
    ON brand_payment_allocations(agreement_id, version_no, fee_id);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
