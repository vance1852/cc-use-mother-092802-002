import json
import unittest

from beverage_ops_foundation.api import route
from beverage_ops_foundation.brand_service import BrandService
from beverage_ops_foundation.storage import Database

RACE = {"starts_at": "2026-11-10T12:00:00+08:00", "ends_at": "2026-11-10T20:00:00+08:00"}


class BrandApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = BrandService(self.database)
        self._call("POST", "/organizations",
                   {"request_id": "org", "organization_id": "o1", "name": "品牌方"},
                   "bootstrap")
        self._call("POST", "/actors",
                   {"request_id": "r-admin", "new_actor_id": "admin-1", "display_name": "管理员",
                    "role": "admin", "organization_id": "o1"}, "bootstrap")
        for rid, aid, name, role in (
            ("r-legal", "legal-1", "法务", "legal"),
            ("r-mkt", "mkt-1", "市场", "marketing"),
            ("r-fin", "fin-1", "财务", "finance"),
        ):
            self._call("POST", "/actors",
                       {"request_id": rid, "new_actor_id": aid, "display_name": name,
                        "role": role, "organization_id": "o1"}, "admin-1")

    def tearDown(self):
        self.database.close()

    def _call(self, method, path, body=None, actor="legal-1"):
        return route(self.service, method, path, body or {}, {"X-Actor-Id": actor})

    def _plan(self, agreement_id: str, partner: str, region="CN-SH", placement="main-barrier"):
        self._call("POST", "/brand/agreements",
                   {"request_id": f"r-{agreement_id}", "agreement_id": agreement_id,
                    "partner_id": partner, "partner_name": partner})
        self._call("POST", "/brand/sessions",
                   {"request_id": f"r-{agreement_id}-s", "agreement_id": agreement_id,
                    "session_id": f"s-{agreement_id}", "event_type": "racing",
                    "name": "赛车日", "region_code": region, "venue": "赛车场", **RACE}, "mkt-1")
        self._call("POST", "/brand/entitlements",
                   {"request_id": f"r-{agreement_id}-e", "agreement_id": agreement_id,
                    "entitlement_id": f"e-{agreement_id}", "kind": "exposure_position",
                    "title": "主屏障", "region_code": region, "session_id": f"s-{agreement_id}",
                    "placement_code": placement, "category_code": "beer", "exclusive": True}, "mkt-1")
        self._call("POST", "/brand/fees",
                   {"request_id": f"r-{agreement_id}-f", "agreement_id": agreement_id,
                    "fee_id": f"f-{agreement_id}", "entitlement_id": f"e-{agreement_id}",
                    "title": "费用", "amount_minor": 1000000, "currency": "CNY"}, "mkt-1")

    def test_sign_conflict_returns_structured_409(self):
        self._plan("agr-a", "p1")
        status, payload = self._call("POST", "/brand/agreements/agr-a/sign",
                                     {"request_id": "sign-a", "version_no": 1, "signer_name": "法务"})
        self.assertEqual(201, status)
        self._plan("agr-b", "p2")
        status, payload = self._call("POST", "/brand/agreements/agr-b/sign",
                                     {"request_id": "sign-b", "version_no": 1, "signer_name": "法务"})
        self.assertEqual(409, status)
        self.assertEqual("brand_conflict", payload["error"])
        kinds = {item["kind"] for item in payload["conflicts"]}
        self.assertIn("exclusive_category", kinds)
        self.assertIn("exposure_position", kinds)

    def test_finance_cannot_create_agreement(self):
        status, payload = self._call("POST", "/brand/agreements",
                                     {"request_id": "x", "agreement_id": "agr-x",
                                      "partner_id": "p", "partner_name": "p"}, "fin-1")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_marketing_sees_agreement_but_not_amounts(self):
        self._plan("agr-a", "p1")
        status, payload = self._call("GET", "/brand/agreements/agr-a", actor="mkt-1")
        self.assertEqual(200, status)
        self.assertFalse(payload["amounts_visible"])
        self.assertIsNone(payload["fees"][0]["amount_minor"])
        status, payload = self._call("GET", "/brand/agreements/agr-a/settlement", actor="mkt-1")
        self.assertEqual(403, status)

    def test_idempotent_replay_returns_200(self):
        body = {"request_id": "agr-i", "agreement_id": "agr-i", "partner_id": "pi", "partner_name": "P"}
        first = self._call("POST", "/brand/agreements", body)
        replay = self._call("POST", "/brand/agreements", body)
        self.assertEqual(201, first[0])
        self.assertEqual(200, replay[0])
        self.assertFalse(first[1]["replayed"])
        self.assertTrue(replay[1]["replayed"])

    def test_payment_workflow_and_explanation(self):
        self._plan("agr-a", "p1", region="CN-BJ")
        self._call("POST", "/brand/agreements/agr-a/sign",
                   {"request_id": "sign-a", "version_no": 1, "signer_name": "法务"})
        status, payload = self._call("POST", "/brand/evidence",
                                     {"request_id": "ev-1", "agreement_id": "agr-a",
                                      "entitlement_id": "e-agr-a", "filename": "a.jpg",
                                      "content_hash": "h1", "media_type": "image/jpeg",
                                      "size_bytes": 10}, "mkt-1")
        self.assertEqual(201, status)
        evidence_id = payload["resource_id"]
        status, payload = self._call("POST", "/brand/acceptances",
                                     {"request_id": "acc-1", "evidence_id": evidence_id,
                                      "decision": "accepted"}, "mkt-1")
        acceptance_id = payload["resource_id"]
        status, payload = self._call("POST", "/brand/payments",
                                     {"request_id": "pay-1", "agreement_id": "agr-a",
                                      "payment_id": "pay-1", "currency": "CNY",
                                      "amount_minor": 1000000,
                                      "allocations": [{"fee_id": "f-agr-a", "amount_minor": 1000000,
                                                       "acceptance_ids": [acceptance_id],
                                                       "evidence_ids": [evidence_id]}]}, "fin-1")
        self.assertEqual(201, status, payload)
        status, explanation = self._call("GET", "/brand/payments/pay-1", actor="fin-1")
        self.assertEqual(200, status)
        self.assertEqual(1000000, explanation["amount_minor"])
        alloc = explanation["allocations"][0]
        self.assertEqual("f-agr-a", alloc["fee_id"])
        self.assertEqual(acceptance_id, alloc["acceptances"][0]["acceptance_id"])
        self.assertEqual("h1", alloc["evidence"][0]["content_hash"])
        # 市场可以看解释但看不到金额
        status, mkt_view = self._call("GET", "/brand/payments/pay-1", actor="mkt-1")
        self.assertEqual(200, status)
        self.assertNotIn("amount_minor", mkt_view)
        self.assertNotIn("amount_minor", mkt_view["allocations"][0])

    def test_duplicate_evidence_http_replays(self):
        self._plan("agr-a", "p1", region="CN-BJ")
        self._call("POST", "/brand/agreements/agr-a/sign",
                   {"request_id": "sign-a", "version_no": 1, "signer_name": "法务"})
        body = {"agreement_id": "agr-a", "entitlement_id": "e-agr-a", "filename": "a.jpg",
                "content_hash": "dup-hash", "media_type": "image/jpeg", "size_bytes": 1}
        first = self._call("POST", "/brand/evidence", {"request_id": "ev-d1", **body}, "mkt-1")
        second = self._call("POST", "/brand/evidence", {"request_id": "ev-d2", **body}, "mkt-1")
        self.assertEqual(first[1]["resource_id"], second[1]["resource_id"])
        status, tracking = self._call(
            "GET", "/brand/agreements/agr-a/entitlements/e-agr-a/tracking", actor="mkt-1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(tracking["evidence"]))

    def test_unknown_brand_route_is_404(self):
        status, payload = route(self.service, "GET", "/brand/nope", {}, {"X-Actor-Id": "legal-1"})
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
