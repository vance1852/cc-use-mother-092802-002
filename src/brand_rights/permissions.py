"""法务、市场、财务三类后台角色的可见范围与操作权限。"""

from __future__ import annotations

from typing import Any

# 角色 -> 可写动作
WRITE_MATRIX: dict[str, frozenset[str]] = {
    "admin": frozenset({
        "agreement.write", "agreement.sign", "agreement.terminate",
        "right.write", "session.write", "asset.write", "deliverable.write",
        "evidence.upload", "acceptance.decide",
        "dispute.raise", "dispute.resolve",
        "fee.write", "payment.write",
    }),
    # 法务：协议全生命周期、排他范围、争议裁决
    "legal": frozenset({
        "agreement.write", "agreement.sign", "agreement.terminate",
        "right.write", "dispute.raise", "dispute.resolve",
    }),
    # 市场：权益计划、场次、媒体与物料、现场证据与验收
    "marketing": frozenset({
        "right.write", "session.write", "asset.write", "deliverable.write",
        "evidence.upload", "acceptance.decide", "dispute.raise",
    }),
    "reviewer": frozenset({
        "evidence.upload", "acceptance.decide", "dispute.raise",
    }),
    # 财务：费用承诺与付款
    "finance": frozenset({
        "fee.write", "payment.write",
    }),
}

# 角色 -> 可读取的资源族
READ_MATRIX: dict[str, frozenset[str]] = {
    "admin": frozenset({"agreement", "right", "operations", "dispute", "finance"}),
    "auditor": frozenset({"agreement", "right", "operations", "dispute", "finance"}),
    "legal": frozenset({"agreement", "right", "dispute", "operations"}),
    "marketing": frozenset({"agreement", "right", "operations", "dispute"}),
    "reviewer": frozenset({"agreement", "right", "operations", "dispute"}),
    "finance": frozenset({"agreement", "right", "operations", "finance"}),
    "operator": frozenset({"agreement", "right", "operations"}),
}

# 付款解释中按角色隐去的字段
PAYMENT_HIDDEN_FIELDS: dict[str, frozenset[str]] = {
    "finance": frozenset(),
    "admin": frozenset(),
    "auditor": frozenset(),
}


def can_write(role: str, action: str) -> bool:
    return action in WRITE_MATRIX.get(role, frozenset())


def can_read(role: str, family: str) -> bool:
    return family in READ_MATRIX.get(role, frozenset())


def filter_payment_view(role: str, payment_view: dict[str, Any]) -> dict[str, Any]:
    """无权看财务明细的角色整体拿不到付款接口；此函数保留给将来字段级裁剪。"""

    hidden = PAYMENT_HIDDEN_FIELDS.get(role, frozenset({"amount_minor", "allocations"}))
    if not hidden:
        return payment_view
    return {key: value for key, value in payment_view.items() if key not in hidden}
