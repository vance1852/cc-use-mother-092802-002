"""权益之间的时间与范围冲突检测（纯函数，便于离线测试）。

权利（right）以字典形式参与计算，需要以下键：
right_id / agreement_id / right_kind / event_type / region / venue /
window_start / window_end（ISO UTC 字符串或 None）/ spec（dict），
排他类权利另外带 exclusivity（dict 或 None）。

只检测不同协议（即不同合作方）之间的冲突；同一协议内的多条权利互为
补充，不视为冲突。
"""

from __future__ import annotations

from typing import Any, Iterable

GLOBAL = "global"


def windows_overlap(start_a: str | None, end_a: str | None,
                    start_b: str | None, end_b: str | None) -> bool:
    """半开区间时间窗是否重叠；None 表示对应方向无界。ISO UTC 串可字典序比较。"""

    if start_a and end_b and start_a >= end_b:
        return False
    if start_b and end_a and start_b >= end_a:
        return False
    return True


def _scope_matches_right(scope_type: str, scope_value: str, right: dict[str, Any]) -> bool:
    """排他范围是否覆盖某条现场权利。"""

    if scope_type == GLOBAL:
        return True
    if scope_type == "region":
        return bool(scope_value) and right.get("region") == scope_value
    if scope_type == "venue":
        return bool(scope_value) and right.get("venue") == scope_value
    if scope_type == "event_type":
        return bool(scope_value) and right.get("event_type") == scope_value
    if scope_type == "event":
        spec = right.get("spec") or {}
        return bool(scope_value) and spec.get("target_session_id") == scope_value
    return False


def _exclusive_scopes_overlap(one: dict[str, Any], other: dict[str, Any]) -> bool:
    """两个排他范围是否相互覆盖。"""

    if one["scope_type"] == GLOBAL or other["scope_type"] == GLOBAL:
        return True
    if one["scope_type"] == other["scope_type"]:
        return one["scope_value"] == other["scope_value"]
    # 不同粒度的范围无法在缺少地理层级表时稳妥判定，按不重叠处理，
    # 其与现场权利的冲突仍会由“排他对现场权利”规则捕获。
    return False


def _conflict_record(rule: str, left: dict[str, Any], right: dict[str, Any],
                     reason: str) -> dict[str, Any]:
    return {
        "rule": rule,
        "reason": reason,
        "right_id": left["right_id"],
        "agreement_id": left["agreement_id"],
        "other_right_id": right["right_id"],
        "other_agreement_id": right["agreement_id"],
    }


def detect_conflicts(candidate: Iterable[dict[str, Any]],
                     committed: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """对比待签署版本的候选权利与其他已签署协议的现行权利，返回冲突明细。"""

    candidate = list(candidate)
    committed = list(committed)
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(record: dict[str, Any]) -> None:
        key = (record["rule"], record["right_id"], record["other_right_id"],
               record["other_agreement_id"])
        if key not in seen:
            seen.add(key)
            findings.append(record)

    candidate_exclusive = [r for r in candidate if r.get("exclusivity")]
    committed_exclusive = [r for r in committed if r.get("exclusivity")]

    for left in candidate_exclusive:
        lex = left["exclusivity"]
        # 规则一：两家合作方就同一品类拿到互相覆盖的排他权
        for right in committed_exclusive:
            if right["agreement_id"] == left["agreement_id"]:
                continue
            rex = right["exclusivity"]
            if lex["category_label"] != rex["category_label"]:
                continue
            if not _exclusive_scopes_overlap(lex, rex):
                continue
            if not windows_overlap(lex.get("window_start"), lex.get("window_end"),
                                   rex.get("window_start"), rex.get("window_end")):
                continue
            add(_conflict_record(
                "exclusive_overlap", left, right,
                f"品类 {lex['category_label']} 的排他范围与时间窗同时覆盖",
            ))
        # 规则二：排他权覆盖了另一合作方已承诺的现场/供货/物料权利
        for other in committed:
            if other["agreement_id"] == left["agreement_id"]:
                continue
            if other.get("exclusivity"):
                continue
            if not _scope_matches_right(lex["scope_type"], lex["scope_value"], other):
                continue
            if not windows_overlap(lex.get("window_start"), lex.get("window_end"),
                                   other.get("window_start"), other.get("window_end")):
                continue
            add(_conflict_record(
                "exclusive_vs_presence", left, other,
                f"品类 {lex['category_label']} 的排他权覆盖了对方既有的 {other['right_kind']} 权益",
            ))

    # 规则二（反向）：其他合作方已签署的全局/区域排他权，覆盖候选版本里的现场权利
    for other in committed_exclusive:
        rex = other["exclusivity"]
        for left in candidate:
            if left["agreement_id"] == other["agreement_id"] or left.get("exclusivity"):
                continue
            if not _scope_matches_right(rex["scope_type"], rex["scope_value"], left):
                continue
            if not windows_overlap(rex.get("window_start"), rex.get("window_end"),
                                   left.get("window_start"), left.get("window_end")):
                continue
            add(_conflict_record(
                "exclusive_vs_presence", other, left,
                f"对方品类 {rex['category_label']} 的既有排他权覆盖了本版本的 {left['right_kind']} 权益",
            ))

    # 规则三：同一现场位置的同一广告位在重叠时间被卖给两家
    candidate_slots = [r for r in candidate if r["right_kind"] == "exposure"]
    committed_slots = [r for r in committed if r["right_kind"] == "exposure"]
    for left in candidate_slots:
        slot = (left.get("spec") or {}).get("placement_slot")
        if not slot or not left.get("venue"):
            continue
        for right in committed_slots:
            if right["agreement_id"] == left["agreement_id"]:
                continue
            if right.get("venue") != left["venue"]:
                continue
            if (right.get("spec") or {}).get("placement_slot") != slot:
                continue
            if not windows_overlap(left.get("window_start"), left.get("window_end"),
                                   right.get("window_start"), right.get("window_end")):
                continue
            add(_conflict_record(
                "placement_double_sale", left, right,
                f"场地 {left['venue']} 的广告位 {slot} 在重叠时间内重复承诺",
            ))

    return findings


def sessions_overlap(venue_a: str, start_a: str, end_a: str,
                     venue_b: str, start_b: str, end_b: str) -> bool:
    """同一场地的两个场次时间窗是否重叠。"""

    return bool(venue_a) and venue_a == venue_b and windows_overlap(start_a, end_a, start_b, end_b)
