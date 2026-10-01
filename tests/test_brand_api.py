"""品牌权益 HTTP 路由测试。"""

import unittest
from datetime import datetime, timezone

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database

from brand_rights.api import route
from brand_rights.service import BrandRightsService


class BrandApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 5, 1, tzinfo=timezone.utc))
        self.base = DomainService(self.database, clock)
        self.service = BrandRightsService(self.database, clock)
        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="品牌方")
        self.base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                 display_name="管理员", role="admin", organization_id="o1")
        for req, aid, role in [("legal", "l1", "legal"), ("mkt", "m1", "marketing"),
                               ("fin", "f1", "finance")]:
            self.base.register_actor(request_id=req, actor_id="a1", new_actor_id=aid,
                                     display_name=role, role=role, organization_id="o1")

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor="l1"):
        return route(self.service, method, path, body or {}, {"X-Actor-Id": actor})

    def _ready_agreement(self, agreement="ag-1"):
        self.call("POST", "/brand/agreements", {
            "request_id": f"create-{agreement}", "agreement_id": agreement,
            "partner_id": "p1", "partner_name": "伙伴A"})
        self.call("POST", "/brand/agreement-versions", {
            "request_id": f"ver-{agreement}", "agreement_id": agreement,
            "document_ref": "doc.pdf", "content": {"terms": 1}})
        status, payload = self.call("POST", "/brand/rights", {
            "request_id": f"right-{agreement}", "agreement_id": agreement, "version_no": 1,
            "event_type": "racing", "right_kind": "exposure", "title": "车身商标",
            "venue": "赛车场", "window_start": "2026-06-10T00:00:00Z",
            "window_end": "2026-06-11T00:00:00Z"})
        return payload["resource_id"]

    def test_full_flow_over_http(self):
        right_id = self._ready_agreement()
        status, payload = self.call("POST", "/brand/agreements/sign", {
            "request_id": "sign", "agreement_id": "ag-1", "version_no": 1})
        self.assertEqual(200, status)
        self.assertFalse(payload["replayed"])

        status, payload = self.call("POST", "/brand/fees", {
            "request_id": "fee", "right_id": right_id, "label": "验收费",
            "amount_minor": 100000, "currency": "CNY", "due_kind": "per_acceptance"}, actor="f1")
        self.assertEqual(201, status)

        status, payload = self.call("POST", "/brand/evidence", {
            "request_id": "ev", "right_id": right_id, "evidence_kind": "照片",
            "file_ref": "s3://1.jpg", "content_hash": "abc123"}, actor="m1")
        self.assertEqual(201, status)
        evidence_id = payload["resource_id"]

        status, payload = self.call("POST", "/brand/acceptance", {
            "request_id": "acc", "evidence_id": evidence_id, "decision": "accepted"}, actor="m1")
        self.assertEqual(201, status)

        fee_id = self.database.connection.execute(
            "SELECT fee_id FROM br_fee_items").fetchone()["fee_id"]
        status, payload = self.call("POST", "/brand/payments", {
            "request_id": "pay", "agreement_id": "ag-1", "amount_minor": 100000,
            "currency": "CNY", "allocations": [
                {"fee_id": fee_id, "amount_minor": 100000, "evidence_id": evidence_id}]}, actor="f1")
        self.assertEqual(201, status)
        payment_id = payload["resource_id"]
        self.call("POST", "/brand/payments/complete",
                  {"request_id": "done", "payment_id": payment_id}, actor="f1")

        status, payload = self.call("GET", f"/brand/payments/{payment_id}", actor="f1")
        self.assertEqual(200, status)
        self.assertEqual(100000, payload["total_explained_minor"])
        self.assertEqual("accepted", payload["allocations"][0]["evidence"]["decision"])

    def test_conflict_returned_as_409_with_findings(self):
        # 先签一份全局排他
        self._ready_agreement()
        self.call("POST", "/brand/rights", {
            "request_id": "ex1", "agreement_id": "ag-1", "version_no": 1,
            "event_type": "racing", "right_kind": "exclusive_category", "title": "啤酒排他",
            "exclusivity": {"category_label": "啤酒", "scope_type": "global",
                            "window_start": "2026-06-01T00:00:00Z",
                            "window_end": "2026-09-01T00:00:00Z"}})
        self.call("POST", "/brand/agreements/sign",
                  {"request_id": "sign1", "agreement_id": "ag-1", "version_no": 1})
        # 第二份协议的区域现场权利落在同一时间窗
        self._ready_agreement("ag-2")
        self.call("POST", "/brand/rights", {
            "request_id": "ex2", "agreement_id": "ag-2", "version_no": 1,
            "event_type": "football", "right_kind": "exposure", "title": "球场展位",
            "region": "华东",
            "window_start": "2026-07-01T00:00:00Z",
            "window_end": "2026-07-02T00:00:00Z"})
        status, payload = self.call("POST", "/brand/agreements/sign",
                                    {"request_id": "sign2", "agreement_id": "ag-2", "version_no": 1})
        self.assertEqual(409, status)
        self.assertEqual("plan_conflict", payload["error"])
        self.assertTrue(payload["findings"])

    def test_finance_cannot_list_operations_only_payments(self):
        status, payload = self.call("GET", "/brand/agreements", actor="f1")
        self.assertEqual(200, status)
        # 市场不能看付款解释
        status, payload = self.call("GET", "/brand/payments/whatever", actor="m1")
        self.assertEqual(403, status)

    def test_unknown_brand_route_404(self):
        status, payload = self.call("GET", "/brand/nope")
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
