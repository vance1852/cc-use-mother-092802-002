"""品牌权益领域服务的端到端规则测试。"""

import unittest
from datetime import datetime, timezone

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database

from brand_rights.errors import PlanConflictError
from brand_rights.service import BrandRightsService


def iso(month, day, hour=0):
    return f"2026-{month:02d}-{day:02d}T{hour:02d}:00:00Z"


class BrandRightsServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 5, 1, tzinfo=timezone.utc))
        self.base = DomainService(self.database, clock)
        self.service = BrandRightsService(self.database, clock)
        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="品牌方")
        self.base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                 display_name="管理员", role="admin", organization_id="o1")
        for req, aid, name, role in [
            ("legal", "l1", "法务", "legal"),
            ("mkt", "m1", "市场", "marketing"),
            ("fin", "f1", "财务", "finance"),
            ("audit", "au1", "审计", "auditor"),
        ]:
            self.base.register_actor(request_id=req, actor_id="a1", new_actor_id=aid,
                                     display_name=name, role=role, organization_id="o1")

    def tearDown(self):
        self.database.close()

    # -- 工具 -------------------------------------------------------------

    def _agreement(self, req, agreement_id, partner, actor="l1", renewal=None):
        return self.service.create_agreement(
            request_id=req, actor_id=actor, agreement_id=agreement_id,
            partner_id=f"p-{agreement_id}", partner_name=partner,
            renewal_of_agreement_id=renewal).resource_id

    def _version(self, req, agreement_id, actor="l1"):
        self.service.add_version(request_id=req, actor_id=actor,
                                 agreement_id=agreement_id,
                                 document_ref=f"docs/{agreement_id}/v1.pdf",
                                 content={"clauses": ["标准条款"]})
        return 1

    def _right(self, req, agreement_id, *, actor="l1", version=1, kind="exposure",
               event_type="racing", title="露出", **kwargs):
        return self.service.add_right(
            request_id=req, actor_id=actor, agreement_id=agreement_id, version_no=version,
            event_type=event_type, right_kind=kind, title=title, **kwargs).resource_id

    # -- 协议与版本 -------------------------------------------------------

    def test_marketing_cannot_sign_legal_owns_signature(self):
        self._agreement("ag1", "ag-1", "赛车伙伴")
        self._version("v1", "ag-1")
        self._right("r1", "ag-1", title="车身商标")
        with self.assertRaises(PermissionDenied):
            self.service.sign_agreement(request_id="sign", actor_id="m1",
                                        agreement_id="ag-1", version_no=1)

    def test_sign_then_version_is_immutable(self):
        self._agreement("ag1", "ag-1", "赛车伙伴")
        self._version("v1", "ag-1")
        self._right("r1", "ag-1", title="车身商标")
        self.service.sign_agreement(request_id="sign", actor_id="l1",
                                    agreement_id="ag-1", version_no=1)
        with self.assertRaises(ConflictError):
            self.service.add_right(request_id="r2", actor_id="l1", agreement_id="ag-1",
                                   version_no=1, event_type="racing", right_kind="exposure",
                                   title="赛后再改")

    def test_conflict_blocks_signing_but_preview_does_not(self):
        # 伙伴 A：华东啤酒区域排他（6-9 月）
        self._agreement("agA", "ag-A", "伙伴A")
        self._version("vA", "ag-A")
        self._right("rA", "ag-A", kind="exclusive_category", title="啤酒排他",
                    exclusivity={"category_label": "啤酒", "scope_type": "region",
                                 "scope_value": "华东", "window_start": iso(6, 1),
                                 "window_end": iso(9, 1)})
        self.service.sign_agreement(request_id="signA", actor_id="l1",
                                    agreement_id="ag-A", version_no=1)
        # 伙伴 B：同样的华东啤酒排他（8-10 月），签署前必须被拦截
        self._agreement("agB", "ag-B", "伙伴B")
        self._version("vB", "ag-B")
        self._right("rB", "ag-B", kind="exclusive_category", title="啤酒排他",
                    exclusivity={"category_label": "啤酒", "scope_type": "region",
                                 "scope_value": "华东", "window_start": iso(8, 1),
                                 "window_end": iso(10, 1)})
        findings = self.service.preview_conflicts(actor_id="l1", agreement_id="ag-B", version_no=1)
        self.assertEqual(1, len(findings))
        self.assertEqual("exclusive_overlap", findings[0]["rule"])
        with self.assertRaises(PlanConflictError):
            self.service.sign_agreement(request_id="signB", actor_id="l1",
                                        agreement_id="ag-B", version_no=1)

    def test_amendment_creates_new_version_and_old_remains_history(self):
        self._agreement("ag1", "ag-1", "伙伴A")
        self._version("v1", "ag-1")
        self._right("r1", "ag-1", title="露出一")
        self.service.sign_agreement(request_id="sign1", actor_id="l1",
                                    agreement_id="ag-1", version_no=1)
        self.service.add_version(request_id="v2", actor_id="l1", agreement_id="ag-1",
                                 document_ref="docs/ag-1/v2.pdf",
                                 content={"clauses": ["新增音乐节条款"]}, change_note="增补")
        self._right("r2", "ag-1", version=2, event_type="music_festival", title="音乐节展位")
        self.service.sign_agreement(request_id="sign2", actor_id="l1",
                                    agreement_id="ag-1", version_no=2)
        detail = self.service.get_agreement_detail(actor_id="l1", agreement_id="ag-1")
        statuses = {v["version_no"]: v["status"] for v in detail["versions"]}
        self.assertEqual("superseded", statuses[1])
        self.assertEqual("signed", statuses[2])

    # -- 续期 -------------------------------------------------------------

    def test_renewal_copies_rights_but_not_sessions_or_fees(self):
        self._agreement("ag1", "ag-1", "伙伴A")
        self._version("v1", "ag-1")
        right_id = self._right("r1", "ag-1", venue="上海国际赛车场",
                               window_start=iso(6, 10), window_end=iso(6, 11))
        self.service.add_fee(request_id="fee1", actor_id="f1", right_id=right_id,
                             label="赞助费", amount_minor=5000000, currency="CNY",
                             due_kind="fixed")
        self.service.sign_agreement(request_id="sign", actor_id="l1",
                                    agreement_id="ag-1", version_no=1)
        self.service.add_session(request_id="sess", actor_id="m1", source_right_id=right_id,
                                 name="上海站", venue="上海国际赛车场",
                                 starts_at=iso(6, 10, 9), ends_at=iso(6, 10, 18))
        # 续期
        receipt = self.service.renew_agreement(
            request_id="renew", actor_id="l1", new_agreement_id="ag-2",
            source_agreement_id="ag-1", document_ref="docs/ag-2/v1.pdf",
            content={"clauses": ["续期"]})
        self.assertFalse(receipt.replayed)
        detail = self.service.get_agreement_detail(actor_id="l1", agreement_id="ag-2")
        self.assertEqual(1, len(detail["rights"]))
        self.assertEqual(0, len(detail["sessions"]))
        # 法务看不到金额字段；用财务视角确认费用没有被复制
        finance_detail = self.service.get_agreement_detail(actor_id="f1", agreement_id="ag-2")
        self.assertEqual(0, len(finance_detail["fees"]))
        self.assertEqual("ag-1", detail["agreement"]["renewal_of_agreement_id"])
        # 续期版本仍是草稿，旧场次不会自动延续，必须重新安排
        with self.assertRaises(ConflictError):
            self.service.add_session(
                request_id="sess2", actor_id="m1",
                source_right_id=detail["rights"][0]["right_id"],
                name="想直接办的场次", venue="上海国际赛车场",
                starts_at=iso(7, 10, 9), ends_at=iso(7, 10, 18))

    # -- 验收与凭证去重 ---------------------------------------------------

    def _signed_right_with_fee(self, agreement="ag-1", kind="per_acceptance", amount=100000):
        self._agreement(f"ag-{agreement}", agreement, "伙伴A")
        self._version(f"v-{agreement}", agreement)
        right_id = self._right(f"r-{agreement}", agreement, venue="上海国际赛车场",
                               window_start=iso(6, 10), window_end=iso(6, 11))
        self.service.add_fee(request_id=f"fee-{agreement}", actor_id="f1", right_id=right_id,
                             label="按验收付费", amount_minor=amount, currency="CNY",
                             due_kind=kind)
        self.service.sign_agreement(request_id=f"sign-{agreement}", actor_id="l1",
                                    agreement_id=agreement, version_no=1)
        return right_id

    def test_partial_acceptance_pays_proportional_and_return_pays_nothing(self):
        right_id = self._signed_right_with_fee()
        ev = self.service.upload_evidence(
            request_id="ev1", actor_id="m1", right_id=right_id, evidence_kind="现场照片",
            file_ref="s3://ev/1.jpg", content_hash="hash-one")
        self.service.decide_acceptance(request_id="acc1", actor_id="m1",
                                       evidence_id=ev.resource_id, decision="partial",
                                       accepted_ratio=40, note="只完成四成露出")
        fee = self.database.connection.execute(
            "SELECT * FROM br_fee_items WHERE right_id=?", (right_id,)).fetchone()
        self.assertEqual(40000, fee["payable_minor"])
        # 退回补交不产生应付
        ev2 = self.service.upload_evidence(
            request_id="ev2", actor_id="m1", right_id=right_id, evidence_kind="视频",
            file_ref="s3://ev/2.mp4", content_hash="hash-two")
        self.service.decide_acceptance(request_id="acc2", actor_id="m1",
                                       evidence_id=ev2.resource_id, decision="returned",
                                       note="画面无品牌标识，补交")
        fee = self.database.connection.execute(
            "SELECT * FROM br_fee_items WHERE right_id=?", (right_id,)).fetchone()
        self.assertEqual(40000, fee["payable_minor"])
        # 补交后同一内容再次验收只按最新决策算一次，不翻倍
        self.service.decide_acceptance(request_id="acc3", actor_id="m1",
                                       evidence_id=ev2.resource_id, decision="accepted")
        fee = self.database.connection.execute(
            "SELECT * FROM br_fee_items WHERE right_id=?", (right_id,)).fetchone()
        self.assertEqual(140000, fee["payable_minor"])  # 40% + 100% 两张凭证
        events = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM br_acceptance_events").fetchone()["c"]
        self.assertEqual(3, events)

    def test_duplicate_content_hash_cannot_create_second_acceptance(self):
        right_id = self._signed_right_with_fee()
        first = self.service.upload_evidence(
            request_id="ev1", actor_id="m1", right_id=right_id, evidence_kind="照片",
            file_ref="s3://ev/1.jpg", content_hash="same-hash")
        again = self.service.upload_evidence(
            request_id="ev1b", actor_id="m1", right_id=right_id, evidence_kind="照片",
            file_ref="s3://ev/1-copy.jpg", content_hash="same-hash")
        # 新的 request_id 不是请求重放，但内容去重仍返回同一条凭证
        self.assertFalse(again.replayed)
        self.assertEqual(first.resource_id, again.resource_id)
        self.service.decide_acceptance(request_id="acc1", actor_id="m1",
                                       evidence_id=first.resource_id, decision="accepted")
        # 换 request_id、换文件名再传一次同一内容，仍然只是同一条凭证
        third = self.service.upload_evidence(
            request_id="ev1c", actor_id="m1", right_id=right_id, evidence_kind="照片",
            file_ref="s3://ev/1-copy-2.jpg", content_hash="same-hash")
        self.assertFalse(third.replayed)
        self.assertEqual(first.resource_id, third.resource_id)
        fee = self.database.connection.execute(
            "SELECT * FROM br_fee_items WHERE right_id=?", (right_id,)).fetchone()
        self.assertEqual(100000, fee["payable_minor"])

    def test_same_evidence_cannot_support_other_right(self):
        right_a = self._signed_right_with_fee("ag-1")
        right_b = self._signed_right_with_fee("ag-2")
        self.service.upload_evidence(request_id="ev1", actor_id="m1", right_id=right_a,
                                     evidence_kind="照片", file_ref="s3://ev/1.jpg",
                                     content_hash="shared")
        with self.assertRaises(ConflictError):
            self.service.upload_evidence(request_id="ev2", actor_id="m1", right_id=right_b,
                                         evidence_kind="照片", file_ref="s3://ev/1b.jpg",
                                         content_hash="shared")

    # -- 争议与部分冻结 ---------------------------------------------------

    def test_dispute_freezes_only_related_fee_other_right_still_pays(self):
        # 同一协议两条权益，各自一笔按验收费用
        self._agreement("ag1", "ag-1", "伙伴A")
        self._version("v1", "ag-1")
        right_disputed = self._right("rd", "ag-1", title="有争议的音乐节展位",
                                     event_type="music_festival")
        right_ok = self._right("ro", "ag-1", title="正常的赛车露出")
        self.service.add_fee(request_id="fd", actor_id="f1", right_id=right_disputed,
                             label="展位费", amount_minor=200000, currency="CNY",
                             due_kind="per_acceptance")
        self.service.add_fee(request_id="fo", actor_id="f1", right_id=right_ok,
                             label="露出费", amount_minor=300000, currency="CNY",
                             due_kind="per_acceptance")
        self.service.sign_agreement(request_id="sign", actor_id="l1",
                                    agreement_id="ag-1", version_no=1)
        ev_d = self.service.upload_evidence(request_id="evd", actor_id="m1",
                                            right_id=right_disputed, evidence_kind="照片",
                                            file_ref="s3://d.jpg", content_hash="h-d")
        ev_o = self.service.upload_evidence(request_id="evo", actor_id="m1",
                                            right_id=right_ok, evidence_kind="照片",
                                            file_ref="s3://o.jpg", content_hash="h-o")
        dispute_receipt = self.service.decide_acceptance(
            request_id="acc-d", actor_id="m1", evidence_id=ev_d.resource_id,
            decision="disputed", dispute_reason="展位被竞品占用")
        dispute_id = self.database.connection.execute(
            "SELECT dispute_id FROM br_acceptance_events WHERE acceptance_id=?",
            (dispute_receipt.resource_id,)).fetchone()["dispute_id"]
        self.service.decide_acceptance(request_id="acc-o", actor_id="m1",
                                       evidence_id=ev_o.resource_id, decision="accepted")
        fee_d = self.database.connection.execute(
            "SELECT * FROM br_fee_items WHERE right_id=?", (right_disputed,)).fetchone()
        fee_o = self.database.connection.execute(
            "SELECT * FROM br_fee_items WHERE right_id=?", (right_ok,)).fetchone()
        self.assertEqual("frozen", fee_d["status"])
        self.assertEqual("payable", fee_o["status"])
        self.assertEqual(300000, fee_o["payable_minor"])
        # 争议费用不能付款，正常权益可以结算
        with self.assertRaises(ConflictError):
            self.service.propose_payment(
                request_id="pay-bad", actor_id="f1", agreement_id="ag-1",
                amount_minor=200000, currency="CNY",
                allocations=[{"fee_id": fee_d["fee_id"], "amount_minor": 200000}])
        pay = self.service.propose_payment(
            request_id="pay-ok", actor_id="f1", agreement_id="ag-1",
            amount_minor=300000, currency="CNY",
            allocations=[{"fee_id": fee_o["fee_id"], "amount_minor": 300000,
                          "evidence_id": ev_o.resource_id}])
        self.service.complete_payment(request_id="pay-done", actor_id="f1",
                                      payment_id=pay.resource_id)
        # 裁决只支持 50%：冻结解除，按 50% 计应付
        self.service.resolve_dispute(request_id="resolve", actor_id="l1",
                                     dispute_id=dispute_id,
                                     resolution_note="确认展位半天被占用", granted_ratio=50)
        fee_d = self.database.connection.execute(
            "SELECT * FROM br_fee_items WHERE right_id=?", (right_disputed,)).fetchone()
        self.assertEqual("payable", fee_d["status"])
        self.assertEqual(100000, fee_d["payable_minor"])

    # -- 付款解释 ---------------------------------------------------------

    def test_payment_explains_rights_and_evidence(self):
        right_id = self._signed_right_with_fee(kind="per_acceptance", amount=100000)
        ev = self.service.upload_evidence(request_id="ev1", actor_id="m1", right_id=right_id,
                                          evidence_kind="照片", file_ref="s3://1.jpg",
                                          content_hash="h1")
        self.service.decide_acceptance(request_id="acc1", actor_id="m1",
                                       evidence_id=ev.resource_id, decision="partial",
                                       accepted_ratio=60)
        fee_id = self.database.connection.execute(
            "SELECT fee_id FROM br_fee_items WHERE right_id=?", (right_id,)).fetchone()["fee_id"]
        pay = self.service.propose_payment(
            request_id="pay1", actor_id="f1", agreement_id="ag-1", amount_minor=60000,
            currency="CNY", memo="上海站六成露出",
            allocations=[{"fee_id": fee_id, "amount_minor": 60000,
                          "evidence_id": ev.resource_id}])
        self.service.complete_payment(request_id="done1", actor_id="f1",
                                      payment_id=pay.resource_id)
        explanation = self.service.get_payment_explanation(
            actor_id="f1", payment_id=pay.resource_id)
        self.assertEqual(60000, explanation["total_explained_minor"])
        item = explanation["allocations"][0]
        self.assertEqual("partial", item["evidence"]["decision"])
        self.assertEqual(60, item["evidence"]["accepted_ratio"])
        self.assertEqual("上海国际赛车场", item["venue"])

    # -- 角色可见性 -------------------------------------------------------

    def test_role_visibility_finance_sees_amounts_marketing_does_not(self):
        right_id = self._signed_right_with_fee()
        mkt = self.service.get_agreement_detail(actor_id="m1", agreement_id="ag-1")
        fin = self.service.get_agreement_detail(actor_id="f1", agreement_id="ag-1")
        self.assertNotIn("fees", mkt)
        self.assertIn("fee_status", mkt)
        self.assertIn("fees", fin)
        self.assertEqual(100000, fin["fees"][0]["amount_minor"])
        # 财务不能改权益计划，市场不能碰付款
        with self.assertRaises(PermissionDenied):
            self.service.add_right(request_id="x", actor_id="f1", agreement_id="ag-1",
                                   version_no=1, event_type="racing", right_kind="exposure",
                                   title="财务加的权益")

    def test_legal_cannot_see_payment_amount(self):
        right_id = self._signed_right_with_fee()
        payment_id = None
        with self.assertRaises(PermissionDenied):
            self.service.get_payment_explanation(actor_id="l1", payment_id="nonexistent")

    # -- 物料与供货 -------------------------------------------------------

    def test_deliverable_cumulative_delivery_cannot_exceed_due(self):
        self._agreement("ag1", "ag-1", "伙伴A")
        self._version("v1", "ag-1")
        right_id = self._right("r1", "ag-1", kind="supply", title="啤酒供货")
        d = self.service.add_deliverable(request_id="d1", actor_id="m1", right_id=right_id,
                                         material_type="罐装啤酒", quantity_due=100)
        self.service.sign_agreement(request_id="sign", actor_id="l1",
                                    agreement_id="ag-1", version_no=1)
        self.service.register_delivery(request_id="del1", actor_id="m1",
                                       deliverable_id=d.resource_id, quantity=60)
        self.service.register_delivery(request_id="del2", actor_id="m1",
                                       deliverable_id=d.resource_id, quantity=40)
        row = self.database.connection.execute(
            "SELECT * FROM br_deliverables WHERE deliverable_id=?", (d.resource_id,)).fetchone()
        self.assertEqual("delivered", row["status"])
        with self.assertRaises(ConflictError):
            self.service.register_delivery(request_id="del3", actor_id="m1",
                                           deliverable_id=d.resource_id, quantity=1)

    # -- 媒体与按场次费用 -------------------------------------------------

    def test_media_asset_lifecycle_and_per_session_fee_once(self):
        self._agreement("ag1", "ag-1", "伙伴A")
        self._version("v1", "ag-1")
        right_id = self._right("r1", "ag-1", venue="上海国际赛车场",
                               window_start=iso(6, 10), window_end=iso(6, 12))
        self.service.add_fee(request_id="fee-ps", actor_id="f1", right_id=right_id,
                             label="每场费", amount_minor=80000, currency="CNY",
                             due_kind="per_session")
        self.service.sign_agreement(request_id="sign", actor_id="l1",
                                    agreement_id="ag-1", version_no=1)
        asset = self.service.add_media_asset(request_id="asset1", actor_id="m1",
                                             right_id=right_id, placement="维修区背板",
                                             spec="3m×2m", delivery_deadline=iso(6, 9))
        self.service.update_media_status(request_id="asset-prod", actor_id="m1",
                                         asset_id=asset.resource_id, status="in_production")
        self.service.update_media_status(request_id="asset-del", actor_id="m1",
                                         asset_id=asset.resource_id, status="delivered")
        row = self.database.connection.execute(
            "SELECT status FROM br_media_assets WHERE asset_id=?", (asset.resource_id,)).fetchone()
        self.assertEqual("delivered", row["status"])

        sess = self.service.add_session(request_id="sess1", actor_id="m1",
                                        source_right_id=right_id, name="上海站",
                                        venue="上海国际赛车场",
                                        starts_at=iso(6, 10, 9), ends_at=iso(6, 10, 18))
        self.service.update_session_status(request_id="live", actor_id="m1",
                                           session_id=sess.resource_id, status="live")
        self.service.update_session_status(request_id="done", actor_id="m1",
                                           session_id=sess.resource_id, status="completed")
        fee = self.database.connection.execute(
            "SELECT * FROM br_fee_items WHERE right_id=?", (right_id,)).fetchone()
        self.assertEqual("payable", fee["status"])
        self.assertEqual(80000, fee["payable_minor"])
        # 再次提交完成（新 request_id）不得重复计费
        self.service.update_session_status(request_id="done-again", actor_id="m1",
                                           session_id=sess.resource_id, status="completed")
        fee = self.database.connection.execute(
            "SELECT * FROM br_fee_items WHERE right_id=?", (right_id,)).fetchone()
        self.assertEqual(80000, fee["payable_minor"])

    # -- 审计 -------------------------------------------------------------

    def test_audit_chain_covers_brand_lifecycle(self):
        self._signed_right_with_fee()
        valid, count = self.base.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 5)
        actions = {row["action"] for row in self.base.audit_events()}
        self.assertIn("brand.agreement.signed", actions)
        self.assertIn("brand.fee.added", actions)


if __name__ == "__main__":
    unittest.main()
