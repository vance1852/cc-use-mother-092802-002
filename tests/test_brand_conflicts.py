"""品牌权益冲突检测纯函数测试。"""

import unittest

from brand_rights.conflicts import detect_conflicts, windows_overlap


def exclusive(rid, agreement, category, scope_type, scope_value, start, end,
              event_type="racing"):
    return {
        "right_id": rid, "agreement_id": agreement, "version_no": 1,
        "event_type": event_type, "right_kind": "exclusive_category",
        "title": category, "region": "", "venue": "",
        "window_start": start, "window_end": end, "spec": {},
        "exclusivity": {"category_label": category, "scope_type": scope_type,
                        "scope_value": scope_value, "window_start": start, "window_end": end},
    }


def presence(rid, agreement, kind, *, region="", venue="", start=None, end=None,
             spec=None, event_type="racing"):
    return {
        "right_id": rid, "agreement_id": agreement, "version_no": 1,
        "event_type": event_type, "right_kind": kind, "title": rid,
        "region": region, "venue": venue,
        "window_start": start, "window_end": end, "spec": spec or {},
        "exclusivity": None,
    }


class WindowTest(unittest.TestCase):
    def test_windows_overlap(self):
        self.assertTrue(windows_overlap("2026-06-01T00:00:00Z", "2026-06-03T00:00:00Z",
                                        "2026-06-02T00:00:00Z", "2026-06-04T00:00:00Z"))
        self.assertFalse(windows_overlap("2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z",
                                         "2026-06-02T00:00:00Z", "2026-06-03T00:00:00Z"))
        self.assertTrue(windows_overlap(None, "2026-06-02T00:00:00Z",
                                        "2026-06-01T00:00:00Z", None))
        self.assertFalse(windows_overlap("2026-07-01T00:00:00Z", None,
                                         None, "2026-06-01T00:00:00Z"))


class ConflictEngineTest(unittest.TestCase):
    def test_same_category_region_exclusivity_conflicts(self):
        committed = [exclusive("r1", "A", "啤酒", "region", "华东",
                               "2026-06-01T00:00:00Z", "2026-09-01T00:00:00Z")]
        candidate = [exclusive("r2", "B", "啤酒", "region", "华东",
                               "2026-08-01T00:00:00Z", "2026-10-01T00:00:00Z")]
        findings = detect_conflicts(candidate, committed)
        self.assertEqual(1, len(findings))
        self.assertEqual("exclusive_overlap", findings[0]["rule"])

    def test_different_region_or_window_no_conflict(self):
        committed = [exclusive("r1", "A", "啤酒", "region", "华东",
                               "2026-06-01T00:00:00Z", "2026-09-01T00:00:00Z")]
        candidate = [
            exclusive("r2", "B", "啤酒", "region", "华南",
                      "2026-08-01T00:00:00Z", "2026-10-01T00:00:00Z"),
            exclusive("r3", "B", "啤酒", "region", "华东",
                      "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z"),
        ]
        self.assertEqual([], detect_conflicts(candidate, committed))

    def test_same_partner_never_conflicts(self):
        committed = [exclusive("r1", "A", "啤酒", "global", "",
                               "2026-06-01T00:00:00Z", "2026-09-01T00:00:00Z")]
        candidate = [exclusive("r2", "A", "啤酒", "global", "",
                               "2026-07-01T00:00:00Z", "2026-08-01T00:00:00Z")]
        self.assertEqual([], detect_conflicts(candidate, committed))

    def test_global_exclusive_blocks_other_presence(self):
        committed = [presence("p1", "A", "exposure", venue="某体育场",
                              start="2026-06-01T00:00:00Z", end="2026-06-02T00:00:00Z")]
        candidate = [exclusive("r2", "B", "啤酒", "global", "",
                               "2026-06-01T12:00:00Z", "2026-06-03T00:00:00Z")]
        findings = detect_conflicts(candidate, committed)
        self.assertEqual(["exclusive_vs_presence"], [f["rule"] for f in findings])

    def test_placement_double_sale(self):
        committed = [presence("p1", "A", "exposure", venue="上海国际赛车场",
                              start="2026-06-01T00:00:00Z", end="2026-06-02T00:00:00Z",
                              spec={"placement_slot": "主看台护栏A"})]
        candidate = [presence("p2", "B", "exposure", venue="上海国际赛车场",
                              start="2026-06-01T12:00:00Z", end="2026-06-02T12:00:00Z",
                              spec={"placement_slot": "主看台护栏A"})]
        findings = detect_conflicts(candidate, committed)
        self.assertEqual(["placement_double_sale"], [f["rule"] for f in findings])

    def test_different_slot_not_conflict(self):
        committed = [presence("p1", "A", "exposure", venue="上海国际赛车场",
                              start="2026-06-01T00:00:00Z", end="2026-06-02T00:00:00Z",
                              spec={"placement_slot": "主看台护栏A"})]
        candidate = [presence("p2", "B", "exposure", venue="上海国际赛车场",
                              start="2026-06-01T12:00:00Z", end="2026-06-02T12:00:00Z",
                              spec={"placement_slot": "维修区通道"})]
        self.assertEqual([], detect_conflicts(candidate, committed))


if __name__ == "__main__":
    unittest.main()
