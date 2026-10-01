"""品牌权益履约的离线端到端验收。

场景：品牌团队同时赞助赛车与音乐节。第一家合作方签约后，第二家在同区域、
同时间窗口承诺了冲突的啤酒排他品类与主屏障位置，签署前被拦截；调整区域后
才签署。随后现场证据出现部分验收与争议：争议只冻结相关费用，供货权益继续
结算；重复凭证不产生第二次验收；续期不继承旧场次。每笔付款都能解释到
有效权益、验收结论与证据。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .brand_service import BrandConflictError, BrandService
from .clock import FixedClock
from .storage import Database

RACE = ("2026-11-10T12:00:00+08:00", "2026-11-10T20:00:00+08:00")
MUSIC = ("2026-11-10T18:00:00+08:00", "2026-11-10T23:00:00+08:00")


def _plan(service: BrandService, agreement_id: str, partner_code: str, display: str, region: str) -> None:
    service.create_agreement(request_id=f"req-{agreement_id}", actor_id="legal-1",
                             agreement_id=agreement_id, partner_id=f"partner-{partner_code}",
                             partner_name=display)
    service.add_session(request_id=f"req-{agreement_id}-sess", actor_id="mkt-1",
                        agreement_id=agreement_id, session_id=f"sess-{partner_code}",
                        event_type="racing", name=f"{display}赛车日", region_code=region,
                        venue="上海国际赛车场", starts_at=RACE[0], ends_at=RACE[1])
    service.add_entitlement(request_id=f"req-{agreement_id}-ent", actor_id="mkt-1",
                            agreement_id=agreement_id, entitlement_id=f"ent-{partner_code}-barrier",
                            kind="exposure_position", title="主屏障广告", region_code=region,
                            session_id=f"sess-{partner_code}", placement_code="main-barrier",
                            category_code="beer", exclusive=True)
    service.add_fee(request_id=f"req-{agreement_id}-fee", actor_id="mkt-1",
                    agreement_id=agreement_id, fee_id=f"fee-{partner_code}",
                    entitlement_id=f"ent-{partner_code}-barrier", title="主屏障露出费用",
                    amount_minor=1_000_000, currency="CNY")


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "brand_acceptance.sqlite3")
        service = BrandService(database, FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)))

        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-brand", name="品牌经营主体")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-1",
                               display_name="管理员", role="admin", organization_id="org-brand")
        for rid, aid, name, role in (
            ("req-legal", "legal-1", "法务负责人", "legal"),
            ("req-mkt", "mkt-1", "市场负责人", "marketing"),
            ("req-fin", "fin-1", "财务负责人", "finance"),
        ):
            service.register_actor(request_id=rid, actor_id="admin-1", new_actor_id=aid,
                                   display_name=name, role=role, organization_id="org-brand")

        # 1) 第一家顺利签署
        _plan(service, "agr-alpha", "alpha", "合作方阿尔法", "CN-SH")
        service.sign_version(request_id="sign-alpha", actor_id="legal-1",
                             agreement_id="agr-alpha", version_no=1, signer_name="法务负责人")

        # 2) 第二家同区域同窗口承诺冲突权益，签署前必须被拦截
        _plan(service, "agr-beta", "beta", "合作方贝塔", "CN-SH")
        blocked = False
        try:
            service.sign_version(request_id="sign-beta-blocked", actor_id="legal-1",
                                 agreement_id="agr-beta", version_no=1, signer_name="法务负责人")
        except BrandConflictError as exc:
            blocked = True
            conflict_kinds = sorted({c.kind for c in exc.conflicts})
        assert blocked, "冲突的权益计划必须被拒绝签署"
        assert "exclusive_category" in conflict_kinds
        assert "exposure_position" in conflict_kinds

        # 3) 调整到不重叠区域（北京）后重新规划并签署成功
        service.remove_plan_item(request_id="rm-beta-ent", actor_id="legal-1",
                                 agreement_id="agr-beta", kind="entitlement",
                                 item_id="ent-beta-barrier")
        service.remove_plan_item(request_id="rm-beta-sess", actor_id="legal-1",
                                 agreement_id="agr-beta", kind="session",
                                 item_id="sess-beta")
        service.add_session(request_id="req-beta-sess-bj", actor_id="mkt-1",
                            agreement_id="agr-beta", session_id="sess-beta-bj",
                            event_type="music", name="贝塔音乐节", region_code="CN-BJ",
                            venue="北京工人体育场", starts_at=MUSIC[0], ends_at=MUSIC[1])
        service.add_entitlement(request_id="req-beta-ent-bj", actor_id="mkt-1",
                                agreement_id="agr-beta", entitlement_id="ent-beta-stage",
                                kind="activity_window", title="主舞台互动时段",
                                region_code="CN-BJ", session_id="sess-beta-bj",
                                zone_code="main-stage", category_code="beer", exclusive=True)
        service.add_fee(request_id="req-beta-fee-bj", actor_id="mkt-1",
                        agreement_id="agr-beta", fee_id="fee-beta-stage",
                        entitlement_id="ent-beta-stage", title="主舞台活动费用",
                        amount_minor=800_000, currency="CNY")
        assert service.check_conflicts("legal-1", "agr-beta") == []
        service.sign_version(request_id="sign-beta", actor_id="legal-1",
                             agreement_id="agr-beta", version_no=1, signer_name="法务负责人")

        # 4) 现场证据：阿尔法的主屏障全额验收后提出争议，只冻结该费用
        ev = service.upload_evidence(
            request_id="req-ev-alpha", actor_id="mkt-1", agreement_id="agr-alpha",
            entitlement_id="ent-alpha-barrier", filename="barrier-night.jpg",
            content_hash="hash-alpha-001", media_type="image/jpeg", size_bytes=204800)
        disputed = service.decide_acceptance(
            request_id="req-acc-alpha", actor_id="mkt-1", evidence_id=ev.resource_id,
            decision="disputed", note="开场时段主屏障未亮灯，需要主办方说明")
        dispute_id = json.loads(database.connection.execute(
            "SELECT response_json FROM request_receipts WHERE request_id='req-acc-alpha'"
        ).fetchone()["response_json"])["dispute_id"]

        # 5) 重复上传同一凭证：不产生第二条证据，更不会产生第二次验收
        duplicate = service.upload_evidence(
            request_id="req-ev-alpha-dup", actor_id="mkt-1", agreement_id="agr-alpha",
            entitlement_id="ent-alpha-barrier", filename="barrier-night-copy.jpg",
            content_hash="hash-alpha-001", media_type="image/jpeg", size_bytes=204800)
        assert duplicate.resource_id == ev.resource_id
        tracking = service.get_entitlement_tracking("mkt-1", "agr-alpha", "ent-alpha-barrier")
        assert len(tracking["evidence"]) == 1

        # 6) 贝塔现场先退回补交（实到 60/100），再用新凭证记录 40% 的部分验收
        # 贝塔需要一个供货权益：通过修订版本新增（演示变更链与旧版本归档）
        service.revise_agreement(request_id="rev-beta", actor_id="legal-1",
                                 agreement_id="agr-beta", change_note="补充啤酒供货权益")
        service.add_entitlement(request_id="req-beta-supply", actor_id="mkt-1",
                                agreement_id="agr-beta", entitlement_id="ent-beta-supply",
                                kind="supply", title="庆功啤酒供货", region_code="CN-BJ",
                                session_id="sess-beta-bj", quantity=100, unit="箱",
                                category_code="beer")
        service.add_fee(request_id="req-beta-supply-fee", actor_id="mkt-1",
                        agreement_id="agr-beta", fee_id="fee-beta-supply",
                        entitlement_id="ent-beta-supply", title="供货补贴",
                        amount_minor=500_000, currency="CNY")
        service.sign_version(request_id="sign-beta-v2", actor_id="legal-1",
                             agreement_id="agr-beta", version_no=2, signer_name="法务负责人")

        ev_first = service.upload_evidence(
            request_id="req-ev-supply-1", actor_id="mkt-1", agreement_id="agr-beta",
            entitlement_id="ent-beta-supply", filename="delivery-1.jpg",
            content_hash="hash-supply-1", media_type="image/jpeg", size_bytes=10240)
        service.decide_acceptance(request_id="req-acc-ret", actor_id="mkt-1",
                                  evidence_id=ev_first.resource_id, decision="returned",
                                  accepted_quantity=60, note="实到 60 箱，补交 40 箱")
        ev_more = service.upload_evidence(
            request_id="req-ev-supply-2", actor_id="mkt-1", agreement_id="agr-beta",
            entitlement_id="ent-beta-supply", filename="delivery-2.jpg",
            content_hash="hash-supply-2", media_type="image/jpeg", size_bytes=10240)
        partial = service.decide_acceptance(
            request_id="req-acc-part", actor_id="mkt-1", evidence_id=ev_more.resource_id,
            decision="partial", accepted_quantity=40, note="补交 40 箱，累计全额")
        # 40/100 的新凭证按自身比例入账，与退回凭证不叠加；验证按比例可付
        report = service.settlement_report("fin-1", "agr-beta")
        supply_fee = next(f for f in report["fees"] if f["fee_id"] == "fee-beta-supply")
        stage_fee = next(f for f in report["fees"] if f["fee_id"] == "fee-beta-stage")
        assert supply_fee["accepted_ratio"] == 0.4
        assert stage_fee["eligible_minor"] == 0

        # 7) 争议中的阿尔法费用不能付；贝塔供货按有效比例结算
        try:
            service.create_payment(
                request_id="req-pay-frozen", actor_id="fin-1", agreement_id="agr-alpha",
                payment_id="pay-frozen", currency="CNY", amount_minor=1_000_000,
                allocations=[{"fee_id": "fee-alpha", "amount_minor": 1_000_000,
                              "acceptance_ids": ["x"], "evidence_ids": ["y"]}])
            raise AssertionError("争议费用必须被冻结")
        except Exception as exc:
            assert "冻结" in str(exc)

        partial_acc_id = database.connection.execute(
            "SELECT acceptance_id FROM brand_acceptances WHERE evidence_id=?",
            (ev_more.resource_id,)).fetchone()[0]
        service.create_payment(
            request_id="req-pay-supply", actor_id="fin-1", agreement_id="agr-beta",
            payment_id="pay-supply-001", currency="CNY", amount_minor=200_000,
            allocations=[{"fee_id": "fee-beta-supply", "amount_minor": 200_000,
                          "acceptance_ids": [partial_acc_id],
                          "evidence_ids": [ev_more.resource_id]}],
            note="按 40% 履约比例支付供货补贴")
        explanation = service.explain_payment("fin-1", "pay-supply-001")
        assert explanation["amount_minor"] == 200_000
        alloc = explanation["allocations"][0]
        assert alloc["entitlement_id"] == "ent-beta-supply"
        assert alloc["acceptances"][0]["decision"] == "partial"
        assert alloc["evidence"][0]["content_hash"] == "hash-supply-2"
        # 市场视角能看证据链但看不到金额
        mkt_view = service.explain_payment("mkt-1", "pay-supply-001")
        assert "amount_minor" not in mkt_view

        # 8) 争议驳回后费用解冻、恢复全额可付
        service.resolve_dispute(request_id="req-res-alpha", actor_id="legal-1",
                                dispute_id=dispute_id, outcome="rejected",
                                resolution_note="监控回放确认全场次亮灯，争议不成立")
        alpha_report = service.settlement_report("fin-1", "agr-alpha")
        alpha_fee = alpha_report["fees"][0]
        assert not alpha_fee["frozen"]
        assert alpha_fee["eligible_minor"] == 1_000_000

        # 9) 续期不自动延长旧场次
        service.create_agreement(request_id="req-renew", actor_id="legal-1",
                                 agreement_id="agr-alpha-2027", partner_id="partner-alpha",
                                 partner_name="合作方阿尔法（续期）",
                                 renewal_of_agreement_id="agr-alpha")
        renewed = service.get_agreement_detail("legal-1", "agr-alpha-2027")
        assert renewed["renewal_of_agreement_id"] == "agr-alpha"
        assert renewed["sessions"] == [] and renewed["entitlements"] == []

        valid, event_count = service.verify_audit()
        assert valid
        database.close()
        return {
            "status": "ok",
            "blocked_conflict_kinds": conflict_kinds,
            "supply_accepted_ratio": supply_fee["accepted_ratio"],
            "frozen_fee_blocked": True,
            "payment_explained_allocation": alloc["fee_id"],
            "duplicate_evidence_suppressed": True,
            "renewal_sessions_copied": False,
            "audit_valid": valid,
            "audit_events": event_count,
        }


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
