"""品牌权益领域的业务异常。"""

from __future__ import annotations

from typing import Any

from beverage_ops_foundation.errors import ConflictError


class PlanConflictError(ConflictError):
    """签署或变更前扫描出时间窗与排他范围冲突。"""

    code = "plan_conflict"

    def __init__(self, findings: list[dict[str, Any]]) -> None:
        super().__init__(f"权益计划存在 {len(findings)} 项冲突，禁止签署")
        self.findings = findings
