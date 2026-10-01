"""品牌权益履约领域使用的数据对象。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class BrandAgreement:
    agreement_id: str
    partner_id: str
    partner_name: str
    current_version: int
    status: str
    renewal_of_agreement_id: str | None
    created_by: str
    created_at: str


@dataclass(frozen=True)
class BrandVersion:
    agreement_id: str
    version_no: int
    status: str
    change_note: str
    signed_by: str | None
    signed_at: str | None
    created_by: str
    created_at: str


@dataclass(frozen=True)
class BrandSession:
    agreement_id: str
    version_no: int
    session_id: str
    event_type: str
    name: str
    region_code: str
    venue: str
    starts_at: str
    ends_at: str


@dataclass(frozen=True)
class BrandEntitlement:
    agreement_id: str
    version_no: int
    entitlement_id: str
    session_id: str | None
    region_code: str
    kind: str
    placement_code: str | None
    asset_code: str | None
    zone_code: str | None
    category_code: str | None
    exclusive: bool
    starts_at: str | None
    ends_at: str | None
    quantity: float | None
    unit: str | None
    title: str


@dataclass(frozen=True)
class BrandFee:
    agreement_id: str
    version_no: int
    fee_id: str
    entitlement_id: str
    title: str
    amount_minor: int
    currency: str
    due_at: str | None


@dataclass(frozen=True)
class BrandMaterial:
    agreement_id: str
    version_no: int
    delivery_id: str
    session_id: str | None
    entitlement_id: str | None
    title: str
    quantity: float | None
    unit: str | None
    due_at: str | None
    status: str
    delivered_at: str | None
    confirmed_by: str | None
    confirmed_at: str | None


@dataclass(frozen=True)
class BrandEvidence:
    evidence_id: str
    agreement_id: str
    version_no: int
    entitlement_id: str
    content_hash: str
    filename: str
    media_type: str
    size_bytes: int
    uploaded_by: str
    created_at: str


@dataclass(frozen=True)
class BrandAcceptance:
    acceptance_id: str
    evidence_id: str
    agreement_id: str
    version_no: int
    entitlement_id: str
    decision: str
    accepted_quantity: float | None
    accepted_ratio: float
    note: str
    decided_by: str
    decided_at: str


@dataclass(frozen=True)
class BrandDispute:
    dispute_id: str
    acceptance_id: str
    agreement_id: str
    version_no: int
    entitlement_id: str
    evidence_id: str
    reason: str
    status: str
    resolution_note: str
    created_by: str
    created_at: str
    resolved_by: str | None
    resolved_at: str | None


@dataclass(frozen=True)
class BrandPayment:
    payment_id: str
    agreement_id: str
    currency: str
    amount_minor: int
    note: str
    paid_by: str
    paid_at: str


@dataclass(frozen=True)
class BrandConflict:
    """描述签署或变更前识别出的一对时间/范围冲突。"""

    kind: str
    message: str
    agreement_id: str
    entitlement_id: str
    other_agreement_id: str
    other_entitlement_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
