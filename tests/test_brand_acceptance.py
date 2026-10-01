import unittest

from beverage_ops_foundation.brand_acceptance import run


class BrandAcceptanceTest(unittest.TestCase):
    def test_brand_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertIn("exclusive_category", result["blocked_conflict_kinds"])
        self.assertIn("exposure_position", result["blocked_conflict_kinds"])
        self.assertTrue(result["frozen_fee_blocked"])
        self.assertTrue(result["duplicate_evidence_suppressed"])
        self.assertFalse(result["renewal_sessions_copied"])
        self.assertEqual(0.4, result["supply_accepted_ratio"])


if __name__ == "__main__":
    unittest.main()
