import unittest
from datetime import datetime, timezone

from beverage_ops_foundation.brand_service import BrandConflictError, BrandService
from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.errors import ConflictError, PermissionDenied, ValidationError
from beverage_ops_foundation.storage import Database

RACE = ("2026-11-10T12:00:00+08:00", "2026-11-10T20:00:00+08:00")
MUSIC = ("2026-11-10T18:00:00+08:00", "2026-11-10T23:00:00+08:00")


def _iso(hour: int, minute: int = 0) -> str:
    return f"2026-11-10T{hour:02d}:{minute:02d}:00+08:00"


class BrandServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = BrandService(self.database,
                                    FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc)))
        s = self.service
        s.register_organization(request_id="org", actor_id="bootstrap", organization_id="o1", name="品牌方")
        s.register_actor(request_id="r-admin", actor_id="bootstrap", new_actor_id="admin-1",
                         display_name="管理员", role="admin", organization_id="o1")
        for rid, aid, name, role in (
            ("r-legal", "legal-1", "法务", "legal"),
            ("r-mkt", "mkt-1", "市场", "marketing"),
            ("r-fin", "fin-1", "财务", "finance"),
            ("r-aud", "aud-1", "审计", "auditor"),
        ):
            s.register_actor(request_id=rid, actor_id="admin-1", new_actor_id=aid,
                             display_name=name, role=role, organization_id="o1")

    def tearDown(self):
        self.database.close()

    # ------------------------------------------------------------ 计划与签署

    def _plan_agreement(self, agreement_id: str, partner: str, *, actor="legal-1",
                        region="CN-SH", exclusive=True, placement="main-barrier",
                        window=None, category="beer", display=None):
        s = self.service
        s.create_agreement(request_id=f"req-{agreement_id}", actor_id=actor,
                           agreement_id=agreement_id, partner_id=f"p-{partner}",
                           partner_name=display or f"合作方{partner}")
        s.add_session(request_id=f"req-{agreement_id}-sess", actor_id="mkt-1",
                      agreement_id=agreement_id, session_id=f"sess-{partner}",
                      event_type="racing", name=f"{display or partner}赛车日", region_code=region,
                      venue="上海国际赛车场", starts_at=(window or RACE)[0], ends_at=(window or RACE)[1])
        s.add_entitlement(request_id=f"req-{agreement_id}-ent", actor_id="mkt-1",
                          agreement_id=agreement_id, entitlement_id=f"ent-{partner}-beer",
                          kind="exposure_position", title="主屏障广告", region_code=region,
                          session_id=f"sess-{partner}", placement_code=placement,
                          category_code=category, exclusive=exclusive)
        s.add_fee(request_id=f"req-{agreement_id}-fee", actor_id="mkt-1",
                  agreement_id=agreement_id, fee_id=f"fee-{partner}",
                  entitlement_id=f"ent-{partner}-beer", title="露出费用",
                  amount_minor=1000000, currency="CNY")

    def test_conflicting_exclusivity_blocks_second_partner_signature(self):
        self._plan_agreement("agr-a", "a", display="合作方甲")
        self.service.sign_version(request_id="sign-a", actor_id="legal-1",
                                  agreement_id="agr-a", version_no=1, signer_name="法务")
        self._plan_agreement("agr-b", "b", display="合作方乙")
        with self.assertRaises(BrandConflictError) as caught:
            self.service.sign_version(request_id="sign-b", actor_id="legal-1",
                                      agreement_id="agr-b", version_no=1, signer_name="法务")
        kinds = {conflict.kind for conflict in caught.exception.conflicts}
        self.assertIn("exclusive_category", kinds)
        self.assertIn("exposure_position", kinds)

    def test_non_overlapping_region_or_time_does_not_conflict(self):
        self._plan_agreement("agr-a", "a", display="合作方甲", region="CN-SH")
        self.service.sign_version(request_id="sign-a", actor_id="legal-1",
                                  agreement_id="agr-a", version_no=1, signer_name="法务")
        # 不同区域
        self._plan_agreement("agr-b", "b", display="合作方乙", region="CN-BJ")
        self.service.sign_version(request_id="sign-b", actor_id="legal-1",
                                  agreement_id="agr-b", version_no=1, signer_name="法务")
        detail = self.service.get_agreement_detail("legal-1", "agr-b")
        self.assertEqual("active", detail["status"])

    def test_conflicts_visible_before_signature(self):
        self._plan_agreement("agr-a", "a", display="合作方甲")
        self.service.sign_version(request_id="sign-a", actor_id="legal-1",
                                  agreement_id="agr-a", version_no=1, signer_name="法务")
        self._plan_agreement("agr-b", "b", display="合作方乙")
        conflicts = self.service.check_conflicts("legal-1", "agr-b")
        self.assertTrue(any(c["kind"] == "exclusive_category" for c in conflicts))

    def test_marketing_cannot_sign_and_finance_cannot_plan(self):
        self.service.create_agreement(request_id="agr-x", actor_id="legal-1",
                                      agreement_id="agr-x", partner_id="px", partner_name="X")
        with self.assertRaises(PermissionDenied):
            self.service.sign_version(request_id="sign-x", actor_id="mkt-1",
                                      agreement_id="agr-x", version_no=1, signer_name="市场")
        with self.assertRaises(PermissionDenied):
            self.service.add_session(request_id="sess-x", actor_id="fin-1",
                                     agreement_id="agr-x", session_id="sx", event_type="music",
                                     name="音乐节", region_code="CN-SH", venue="场馆",
                                     starts_at=RACE[0], ends_at=RACE[1])

    def test_revision_copies_plan_and_old_version_becomes_superseded(self):
        self._plan_agreement("agr-a", "a", display="合作方甲")
        self.service.sign_version(request_id="sign-a", actor_id="legal-1",
                                  agreement_id="agr-a", version_no=1, signer_name="法务")
        receipt = self.service.revise_agreement(request_id="rev-a", actor_id="legal-1",
                                                agreement_id="agr-a", change_note="增加音乐节权益")
        self.assertEqual("agr-a", receipt.resource_id)
        detail = self.service.get_agreement_detail("legal-1", "agr-a")
        self.assertEqual(2, detail["current_version"])
        # v2 草稿已复制场次、权益、费用，物料状态重置为 pending
        self.assertEqual(1, len(detail["sessions"]))
        self.assertEqual(1, len(detail["entitlements"]))
        self.assertEqual(1, len(detail["fees"]))
        versions = {v["version_no"]: v["status"] for v in detail["versions"]}
        self.assertEqual("signed", versions[1])
        self.assertEqual("draft", versions[2])
        self.service.sign_version(request_id="sign-a2", actor_id="legal-1",
                                  agreement_id="agr-a", version_no=2, signer_name="法务")
        detail = self.service.get_agreement_detail("legal-1", "agr-a")
        versions = {v["version_no"]: v["status"] for v in detail["versions"]}
        self.assertEqual("superseded", versions[1])
        self.assertEqual("signed", versions[2])

    # ------------------------------------------------------------ 续期

    def test_renewal_does_not_carry_old_sessions(self):
        self._plan_agreement("agr-old", "old", display="老合作方")
        self.service.sign_version(request_id="sign-old", actor_id="legal-1",
                                  agreement_id="agr-old", version_no=1, signer_name="法务")
        self.service.create_agreement(request_id="agr-new", actor_id="legal-1",
                                      agreement_id="agr-new", partner_id="p-new",
                                      partner_name="续约合作方",
                                      renewal_of_agreement_id="agr-old")
        detail = self.service.get_agreement_detail("legal-1", "agr-new")
        self.assertEqual("agr-old", detail["renewal_of_agreement_id"])
        self.assertEqual([], detail["sessions"])
        self.assertEqual([], detail["entitlements"])
        self.assertEqual([], detail["fees"])
        # 旧场次仍只属于旧协议
        old_detail = self.service.get_agreement_detail("legal-1", "agr-old")
        self.assertEqual(1, len(old_detail["sessions"]))

    # ------------------------------------------------------------ 证据与验收

    def _signed_agreement_with_two_entitlements(self, agreement_id="agr-1"):
        s = self.service
        s.create_agreement(request_id=f"{agreement_id}-create", actor_id="legal-1",
                           agreement_id=agreement_id, partner_id="p1", partner_name="合作方")
        s.add_session(request_id=f"{agreement_id}-sess", actor_id="mkt-1",
                      agreement_id=agreement_id, session_id="s1", event_type="music",
                      name="城市音乐节", region_code="CN-SH", venue="滨江公园",
                      starts_at=RACE[0], ends_at=RACE[1])
        for eid, kind, code in (("e-sign", "exposure_position", "led-gate"),
                                ("e-supply", "supply", None)):
            kwargs = dict(request_id=f"{agreement_id}-{eid}", actor_id="mkt-1",
                          agreement_id=agreement_id, entitlement_id=eid, kind=kind,
                          title=eid, region_code="CN-SH", session_id="s1")
            if kind == "exposure_position":
                kwargs.update(placement_code=code)
            else:
                kwargs.update(quantity=100, unit="箱")
            s.add_entitlement(**kwargs)
        s.add_fee(request_id=f"{agreement_id}-fee-sign", actor_id="mkt-1",
                  agreement_id=agreement_id, fee_id="fee-sign", entitlement_id="e-sign",
                  title="门头 LED 费用", amount_minor=1000000, currency="CNY")
        s.add_fee(request_id=f"{agreement_id}-fee-supply", actor_id="mkt-1",
                  agreement_id=agreement_id, fee_id="fee-supply", entitlement_id="e-supply",
                  title="供货补贴", amount_minor=500000, currency="CNY")
        s.add_material(request_id=f"{agreement_id}-mat", actor_id="mkt-1",
                       agreement_id=agreement_id, delivery_id="mat-1", title="品牌围挡",
                       session_id="s1", quantity=20, unit="块")
        s.sign_version(request_id=f"{agreement_id}-sign", actor_id="legal-1",
                       agreement_id=agreement_id, version_no=1, signer_name="法务")

    def test_duplicate_evidence_upload_never_creates_second_acceptance(self):
        self._signed_agreement_with_two_entitlements()
        first = self.service.upload_evidence(
            request_id="ev-1", actor_id="mkt-1", agreement_id="agr-1", entitlement_id="e-sign",
            filename="gate.jpg", content_hash="hash-aaa", media_type="image/jpeg", size_bytes=1024)
        second = self.service.upload_evidence(
            request_id="ev-1-dup", actor_id="mkt-1", agreement_id="agr-1", entitlement_id="e-sign",
            filename="gate-copy.jpg", content_hash="hash-aaa", media_type="image/jpeg", size_bytes=1024)
        self.assertFalse(first.replayed)
        self.assertEqual(first.resource_id, second.resource_id)
        tracking = self.service.get_entitlement_tracking("mkt-1", "agr-1", "e-sign")
        self.assertEqual(1, len(tracking["evidence"]))
        self.service.decide_acceptance(request_id="acc-1", actor_id="mkt-1",
                                       evidence_id=first.resource_id, decision="accepted")
        with self.assertRaises(ConflictError):
            self.service.decide_acceptance(request_id="acc-2", actor_id="mkt-1",
                                           evidence_id=first.resource_id, decision="accepted")
        tracking = self.service.get_entitlement_tracking("mkt-1", "agr-1", "e-sign")
        self.assertEqual(1, len(tracking["acceptances"]))

    def test_partial_acceptance_pays_half_and_returned_evidence_can_be_resubmitted(self):
        self._signed_agreement_with_two_entitlements()
        ev = self.service.upload_evidence(
            request_id="ev-supply", actor_id="mkt-1", agreement_id="agr-1",
            entitlement_id="e-supply", filename="delivery.jpg", content_hash="h1",
            media_type="image/jpeg", size_bytes=10)
        # 先退回补交
        self.service.decide_acceptance(request_id="acc-ret", actor_id="mkt-1",
                                       evidence_id=ev.resource_id, decision="returned",
                                       note="数量不足，请补交")
        report = self.service.settlement_report("fin-1", "agr-1")
        supply_fee = next(f for f in report["fees"] if f["fee_id"] == "fee-supply")
        self.assertEqual(0, supply_fee["eligible_minor"])
        # 补交新凭证并按数量部分验收 50/100
        ev2 = self.service.upload_evidence(
            request_id="ev-supply-2", actor_id="mkt-1", agreement_id="agr-1",
            entitlement_id="e-supply", filename="delivery-2.jpg", content_hash="h2",
            media_type="image/jpeg", size_bytes=10)
        self.service.decide_acceptance(request_id="acc-part", actor_id="mkt-1",
                                       evidence_id=ev2.resource_id, decision="partial",
                                       accepted_quantity=50, note="只到货 50 箱")
        report = self.service.settlement_report("fin-1", "agr-1")
        supply_fee = next(f for f in report["fees"] if f["fee_id"] == "fee-supply")
        self.assertEqual(0.5, supply_fee["accepted_ratio"])
        self.assertEqual(250000, supply_fee["eligible_minor"])
        # 超付被拒绝
        with self.assertRaises(ConflictError):
            self.service.create_payment(
                request_id="pay-over", actor_id="fin-1", agreement_id="agr-1",
                payment_id="pay-over", currency="CNY", amount_minor=300000,
                allocations=[{"fee_id": "fee-supply", "amount_minor": 300000,
                              "acceptance_ids": [], "evidence_ids": []}])

    def test_dispute_freezes_only_related_fee_other_entitlements_keep_settling(self):
        self._signed_agreement_with_two_entitlements()
        # e-sign 全额验收后立即提出争议
        ev_sign = self.service.upload_evidence(
            request_id="ev-sign", actor_id="mkt-1", agreement_id="agr-1",
            entitlement_id="e-sign", filename="sign.jpg", content_hash="h-sign",
            media_type="image/jpeg", size_bytes=10)
        disputed = self.service.decide_acceptance(
            request_id="acc-sign", actor_id="mkt-1", evidence_id=ev_sign.resource_id,
            decision="disputed", note="门头 LED 实际未点亮")
        self.assertFalse(disputed.replayed)
        # e-supply 正常验收
        ev_supply = self.service.upload_evidence(
            request_id="ev-sup", actor_id="mkt-1", agreement_id="agr-1",
            entitlement_id="e-supply", filename="sup.jpg", content_hash="h-sup",
            media_type="image/jpeg", size_bytes=10)
        self.service.decide_acceptance(request_id="acc-sup", actor_id="mkt-1",
                                       evidence_id=ev_supply.resource_id, decision="accepted")
        report = self.service.settlement_report("fin-1", "agr-1")
        fees = {f["fee_id"]: f for f in report["fees"]}
        self.assertTrue(fees["fee-sign"]["frozen"])
        self.assertEqual(0, fees["fee-sign"]["eligible_minor"])
        self.assertFalse(fees["fee-supply"]["frozen"])
        self.assertEqual(500000, fees["fee-supply"]["eligible_minor"])
        # 争议费用不能付
        with self.assertRaises(ConflictError):
            self.service.create_payment(
                request_id="pay-frozen", actor_id="fin-1", agreement_id="agr-1",
                payment_id="pay-frozen", currency="CNY", amount_minor=1000000,
                allocations=[{"fee_id": "fee-sign", "amount_minor": 1000000,
                              "acceptance_ids": ["x"], "evidence_ids": ["y"]}])
        # 其他已完成权益继续结算
        supply_acceptance_id = self.database.connection.execute(
            "SELECT acceptance_id FROM brand_acceptances WHERE evidence_id=?",
            (ev_supply.resource_id,)).fetchone()[0]
        self.service.create_payment(
            request_id="pay-supply", actor_id="fin-1", agreement_id="agr-1",
            payment_id="pay-supply", currency="CNY", amount_minor=500000,
            allocations=[{"fee_id": "fee-supply", "amount_minor": 500000,
                          "acceptance_ids": [supply_acceptance_id],
                          "evidence_ids": [ev_supply.resource_id]}],
            note="供货补贴尾款")
        explanation = self.service.explain_payment("fin-1", "pay-supply")
        self.assertEqual(500000, explanation["amount_minor"])
        alloc = explanation["allocations"][0]
        self.assertEqual("e-supply", alloc["entitlement_id"])
        self.assertEqual("accepted", alloc["acceptances"][0]["decision"])
        self.assertEqual("h-sup", alloc["evidence"][0]["content_hash"])

    def test_dispute_resolution_rejected_restores_full_payment(self):
        self._signed_agreement_with_two_entitlements()
        ev = self.service.upload_evidence(
            request_id="ev-x", actor_id="mkt-1", agreement_id="agr-1",
            entitlement_id="e-sign", filename="x.jpg", content_hash="hx",
            media_type="image/jpeg", size_bytes=1)
        receipt = self.service.decide_acceptance(
            request_id="acc-x", actor_id="mkt-1", evidence_id=ev.resource_id,
            decision="disputed", note="待核")
        import json
        dispute_id = json.loads(self.database.connection.execute(
            "SELECT response_json FROM request_receipts WHERE request_id='acc-x'"
        ).fetchone()["response_json"])["dispute_id"]
        self.service.resolve_dispute(request_id="res-x", actor_id="legal-1",
                                     dispute_id=dispute_id, outcome="rejected",
                                     resolution_note="现场复核确认已点亮，驳回争议")
        report = self.service.settlement_report("fin-1", "agr-1")
        fee = next(f for f in report["fees"] if f["fee_id"] == "fee-sign")
        self.assertFalse(fee["frozen"])
        self.assertEqual(1.0, fee["accepted_ratio"])
        self.assertEqual(1000000, fee["eligible_minor"])

    # ------------------------------------------------------------ 物料

    def test_material_delivery_lifecycle(self):
        self._signed_agreement_with_two_entitlements()
        with self.assertRaises(ConflictError):
            self.service.confirm_material(request_id="cfm-early", actor_id="mkt-1",
                                          agreement_id="agr-1", delivery_id="mat-1")
        self.service.mark_material_delivered(request_id="del-1", actor_id="mkt-1",
                                             agreement_id="agr-1", delivery_id="mat-1")
        self.service.confirm_material(request_id="cfm-1", actor_id="mkt-1",
                                      agreement_id="agr-1", delivery_id="mat-1")
        detail = self.service.get_agreement_detail("mkt-1", "agr-1")
        self.assertEqual("confirmed", detail["materials"][0]["status"])

    # ------------------------------------------------------------ 角色可见性

    def test_role_based_amount_visibility(self):
        self._signed_agreement_with_two_entitlements()
        mkt = self.service.get_agreement_detail("mkt-1", "agr-1")
        self.assertFalse(mkt["amounts_visible"])
        self.assertIsNone(mkt["fees"][0]["amount_minor"])
        fin = self.service.get_agreement_detail("fin-1", "agr-1")
        self.assertTrue(fin["amounts_visible"])
        self.assertEqual(1000000, fin["fees"][0]["amount_minor"])
        with self.assertRaises(PermissionDenied):
            self.service.settlement_report("mkt-1", "agr-1")
        with self.assertRaises(PermissionDenied):
            self.service.create_payment(
                request_id="pay-no", actor_id="mkt-1", agreement_id="agr-1",
                payment_id="pay-no", currency="CNY", amount_minor=1, allocations=[])

    def test_auditor_reads_everything_but_cannot_write(self):
        self._signed_agreement_with_two_entitlements()
        items = self.service.list_agreements("aud-1")
        self.assertEqual(1, len(items))
        with self.assertRaises(PermissionDenied):
            self.service.upload_evidence(
                request_id="ev-aud", actor_id="aud-1", agreement_id="agr-1",
                entitlement_id="e-sign", filename="a.jpg", content_hash="h",
                media_type="image/jpeg", size_bytes=1)

    def test_cross_event_type_activity_and_media_conflicts_are_detected(self):
        s = self.service
        # 合作方甲：赛车日的主舞台活动窗口 + 电视媒体资产
        s.create_agreement(request_id="agr-c1", actor_id="legal-1",
                           agreement_id="agr-c1", partner_id="p-c1", partner_name="赛车合作方")
        s.add_session(request_id="sess-c1", actor_id="mkt-1", agreement_id="agr-c1",
                      session_id="s1", event_type="racing", name="赛车日", region_code="CN-SH",
                      venue="赛车场", starts_at=RACE[0], ends_at=RACE[1])
        s.add_entitlement(request_id="ent-c1-act", actor_id="mkt-1", agreement_id="agr-c1",
                          entitlement_id="e-act", kind="activity_window", title="主舞台时段",
                          region_code="CN-SH", session_id="s1", zone_code="main-stage")
        s.add_entitlement(request_id="ent-c1-media", actor_id="mkt-1", agreement_id="agr-c1",
                          entitlement_id="e-media", kind="media_asset", title="地方台片头广告",
                          region_code="*", session_id="s1", asset_code="tv-leaderboard-30s")
        s.sign_version(request_id="sign-c1", actor_id="legal-1", agreement_id="agr-c1",
                       version_no=1, signer_name="法务")
        # 合作方乙：当晚重叠的音乐节（18:00-23:00），同分区与同媒体资产
        s.create_agreement(request_id="agr-c2", actor_id="legal-1",
                           agreement_id="agr-c2", partner_id="p-c2", partner_name="音乐节合作方")
        s.add_session(request_id="sess-c2", actor_id="mkt-1", agreement_id="agr-c2",
                      session_id="s2", event_type="music", name="城市音乐节", region_code="CN-SH",
                      venue="滨江", starts_at=MUSIC[0], ends_at=MUSIC[1])
        s.add_entitlement(request_id="ent-c2-act", actor_id="mkt-1", agreement_id="agr-c2",
                          entitlement_id="e-act2", kind="activity_window", title="主舞台时段",
                          region_code="CN-SH", session_id="s2", zone_code="main-stage")
        s.add_entitlement(request_id="ent-c2-media", actor_id="mkt-1", agreement_id="agr-c2",
                          entitlement_id="e-media2", kind="media_asset", title="地方台片头广告",
                          region_code="*", session_id="s2", asset_code="tv-leaderboard-30s")
        conflicts = s.check_conflicts("legal-1", "agr-c2")
        kinds = {c["kind"] for c in conflicts}
        self.assertIn("activity_zone", kinds)
        self.assertIn("media_slot", kinds)

    def test_dispute_upheld_keeps_fee_non_payable(self):
        self._signed_agreement_with_two_entitlements()
        ev = self.service.upload_evidence(
            request_id="ev-u", actor_id="mkt-1", agreement_id="agr-1",
            entitlement_id="e-sign", filename="u.jpg", content_hash="hu",
            media_type="image/jpeg", size_bytes=1)
        receipt = self.service.decide_acceptance(
            request_id="acc-u", actor_id="mkt-1", evidence_id=ev.resource_id,
            decision="disputed", note="待核")
        dispute_id = self.database.connection.execute(
            "SELECT dispute_id FROM brand_disputes WHERE acceptance_id=?",
            (receipt.resource_id,)).fetchone()[0]
        self.service.resolve_dispute(request_id="res-u", actor_id="legal-1",
                                     dispute_id=dispute_id, outcome="upheld",
                                     resolution_note="复核确认权益未交付，供应商担责")
        report = self.service.settlement_report("fin-1", "agr-1")
        fee = next(f for f in report["fees"] if f["fee_id"] == "fee-sign")
        self.assertFalse(fee["frozen"])
        self.assertEqual(0, fee["eligible_minor"])

    def test_audit_chain_covers_brand_lifecycle(self):
        self._signed_agreement_with_two_entitlements()
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 10)


if __name__ == "__main__":
    unittest.main()
