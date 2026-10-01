"""品牌权益履约领域的 SQLite 表结构。

表按三类聚合组织：

- 协议与版本：agreements / agreement_versions，签署前为 draft，签署后版本不可改；
- 权益计划：brand_rights / sessions / deliverables / media_assets / fee_items；
- 履约与结算：evidence / acceptance_events / disputes / payments / payment_allocations。

所有写入都在外层短事务内完成，并复用基础层的哈希串联审计。
"""

from __future__ import annotations

BRAND_RIGHTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS br_agreements (
    agreement_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    partner_id TEXT NOT NULL,
    partner_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','signed','superseded','terminated')),
    current_version_no INTEGER NOT NULL DEFAULT 0,
    renewal_of_agreement_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    signed_at TEXT,
    terminated_at TEXT
);
CREATE TABLE IF NOT EXISTS br_agreement_versions (
    agreement_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','signed','superseded')),
    document_ref TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    change_note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    signed_by TEXT,
    signed_at TEXT,
    PRIMARY KEY (agreement_id, version_no)
);
CREATE TABLE IF NOT EXISTS br_rights (
    right_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('racing','football','music_festival','other')),
    right_kind TEXT NOT NULL CHECK(right_kind IN ('exposure','exclusive_category','supply','activity_window','media_asset','material','other')),
    title TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT '',
    venue TEXT NOT NULL DEFAULT '',
    window_start TEXT,
    window_end TEXT,
    spec_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('planned','active','fulfilled','partial','disputed','cancelled')),
    copied_from_right_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(agreement_id, version_no, right_id)
);
CREATE TABLE IF NOT EXISTS br_exclusivity (
    right_id TEXT PRIMARY KEY REFERENCES br_rights(right_id),
    category_label TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK(scope_type IN ('global','region','venue','event_type','event')),
    scope_value TEXT NOT NULL DEFAULT '',
    window_start TEXT,
    window_end TEXT
);
CREATE TABLE IF NOT EXISTS br_sessions (
    session_id TEXT PRIMARY KEY,
    source_right_id TEXT NOT NULL,
    agreement_id TEXT NOT NULL,
    name TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT '',
    venue TEXT NOT NULL DEFAULT '',
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('scheduled','live','completed','cancelled')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS br_media_assets (
    asset_id TEXT PRIMARY KEY,
    right_id TEXT NOT NULL,
    placement TEXT NOT NULL,
    spec TEXT NOT NULL DEFAULT '',
    delivery_deadline TEXT,
    status TEXT NOT NULL CHECK(status IN ('planned','in_production','delivered','rejected')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS br_deliverables (
    deliverable_id TEXT PRIMARY KEY,
    right_id TEXT NOT NULL,
    material_type TEXT NOT NULL,
    quantity_due INTEGER NOT NULL CHECK(quantity_due >= 0),
    quantity_delivered INTEGER NOT NULL DEFAULT 0 CHECK(quantity_delivered >= 0),
    due_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending','partial','delivered','overdue','returned')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS br_fee_items (
    fee_id TEXT PRIMARY KEY,
    right_id TEXT NOT NULL,
    agreement_id TEXT NOT NULL,
    label TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK(amount_minor >= 0),
    currency TEXT NOT NULL,
    due_kind TEXT NOT NULL CHECK(due_kind IN ('fixed','per_session','per_acceptance')),
    status TEXT NOT NULL CHECK(status IN ('planned','payable','frozen','paid','void')),
    payable_minor INTEGER NOT NULL DEFAULT 0 CHECK(payable_minor >= 0),
    paid_minor INTEGER NOT NULL DEFAULT 0 CHECK(paid_minor >= 0),
    frozen_for_dispute_id TEXT,
    prev_status TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS br_evidence (
    evidence_id TEXT PRIMARY KEY,
    right_id TEXT NOT NULL,
    session_id TEXT,
    evidence_kind TEXT NOT NULL,
    file_ref TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    uploaded_by TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    latest_decision TEXT NOT NULL DEFAULT 'pending'
        CHECK(latest_decision IN ('pending','accepted','partial','returned','disputed')),
    latest_ratio INTEGER NOT NULL DEFAULT 0,
    latest_acceptance_id TEXT,
    UNIQUE(content_hash, right_id)
);
CREATE TABLE IF NOT EXISTS br_acceptance_events (
    acceptance_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL,
    right_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('accepted','partial','returned','disputed')),
    accepted_ratio INTEGER NOT NULL CHECK(accepted_ratio BETWEEN 0 AND 100),
    note TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    supersedes_acceptance_id TEXT,
    dispute_id TEXT
);
CREATE TABLE IF NOT EXISTS br_disputes (
    dispute_id TEXT PRIMARY KEY,
    right_id TEXT NOT NULL,
    evidence_id TEXT,
    fee_id TEXT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','resolved')),
    resolution_note TEXT NOT NULL DEFAULT '',
    raised_by TEXT NOT NULL,
    raised_at TEXT NOT NULL,
    resolved_by TEXT,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS br_payments (
    payment_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK(amount_minor >= 0),
    currency TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('proposed','completed')),
    memo TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS br_payment_allocations (
    payment_id TEXT NOT NULL,
    fee_id TEXT NOT NULL,
    right_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK(amount_minor >= 0),
    evidence_id TEXT,
    acceptance_id TEXT,
    PRIMARY KEY (payment_id, fee_id, right_id, evidence_id, acceptance_id)
);
CREATE INDEX IF NOT EXISTS idx_br_rights_agreement ON br_rights(agreement_id, version_no);
CREATE INDEX IF NOT EXISTS idx_br_sessions_window ON br_sessions(starts_at, ends_at);
CREATE INDEX IF NOT EXISTS idx_br_evidence_right ON br_evidence(right_id);
CREATE INDEX IF NOT EXISTS idx_br_fees_right ON br_fee_items(right_id);
CREATE INDEX IF NOT EXISTS idx_br_acceptance_evidence ON br_acceptance_events(evidence_id);
"""
