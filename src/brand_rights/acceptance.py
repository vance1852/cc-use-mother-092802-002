"""品牌权益履约服务的离线端到端验收。

在临时 SQLite 库中串起：三家合作方协议（赛车 / 区域足球 / 音乐节）、
签署前冲突拦截、部分验收、争议仅冻结相关费用、续期不延续旧场次、
重复凭证不产生第二次验收，以及付款逐笔解释。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database

from .errors import PlanConflictError
from .service import BrandRightsService


def iso(month, day):
    return f"2026-{month:02d}-{day:02d}T00:00:00Z"


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "brand_acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc))
        base = DomainService(database, clock)
        service = BrandRightsService(database, clock)

        base.register_organization(request_id="org", actor_id="bootstrap",
                                   organization_id="org-001", name="示范品牌方")
        base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-1",
                            display_name="管理员", role="admin", organization_id="org-001")
        base.register_actor(request_id="legal", actor_id="admin-1", new_actor_id="legal-1",
                            display_name="法务", role="legal", organization_id="org-001")
        base.register_actor(request_id="mkt", actor_id="admin-1", new_actor_id="mkt-1",
                            display_name="市场", role="marketing", organization_id="org-001")
        base.register_actor(request_id="fin", actor_id="admin-1", new_actor_id="fin-1",
                            display_name="财务", role="finance", organization_id="org-001")

        # 1) 赛车伙伴：赛道护栏露出 + 啤酒区域排他
        service.create_agreement(request_id="ag-race", actor_id="legal-1",
                                 agreement_id="ag-race", partner_id="partner-race",
                                 partner_name="赛车运营方")
        service.add_version(request_id="ver-race", actor_id="legal-1",
                            agreement_id="ag-race", document_ref="contracts/race-v1.pdf",
                            content={"title": "赛车赞助协议"})
        race_right = service.add_right(
            request_id="right-race", actor_id="legal-1", agreement_id="ag-race",
            version_no=1, event_type="racing", right_kind="exposure", title="主看台护栏",
            venue="上海国际赛车场", window_start=iso(6, 10), window_end=iso(6, 12),
            spec={"placement_slot": "护栏A区"}).resource_id
        service.add_right(
            request_id="right-excl", actor_id="legal-1", agreement_id="ag-race",
            version_no=1, event_type="racing", right_kind="exclusive_category",
            title="华东啤酒排他", exclusivity={
                "category_label": "啤酒", "scope_type": "region", "scope_value": "华东",
                "window_start": iso(6, 1), "window_end": iso(9, 1)})
        service.add_fee(request_id="fee-race", actor_id="fin-1", right_id=race_right,
                        label="赛车露出费", amount_minor=1_000_000, currency="CNY",
                        due_kind="per_acceptance")
        service.sign_agreement(request_id="sign-race", actor_id="legal-1",
                               agreement_id="ag-race", version_no=1)

        # 2) 音乐节伙伴想在华东同一窗口拿啤酒排他 —— 必须在签署前被拦截
        service.create_agreement(request_id="ag-music", actor_id="legal-1",
                                 agreement_id="ag-music", partner_id="partner-music",
                                 partner_name="音乐节主办方")
        service.add_version(request_id="ver-music", actor_id="legal-1",
                            agreement_id="ag-music", document_ref="contracts/music-v1.pdf",
                            content={"title": "音乐节赞助协议"})
        service.add_right(
            request_id="right-music-excl", actor_id="legal-1", agreement_id="ag-music",
            version_no=1, event_type="music_festival", right_kind="exclusive_category",
            title="华东啤酒排他", exclusivity={
                "category_label": "啤酒", "scope_type": "region", "scope_value": "华东",
                "window_start": iso(7, 1), "window_end": iso(7, 3)})
        findings = service.preview_conflicts(actor_id="legal-1",
                                             agreement_id="ag-music", version_no=1)
        blocked_sign = False
        try:
            service.sign_agreement(request_id="sign-music-bad", actor_id="legal-1",
                                   agreement_id="ag-music", version_no=1)
        except PlanConflictError:
            blocked_sign = True
        # 去掉冲突权益后协议可以正常签署
        service.add_version(request_id="ver-music-2", actor_id="legal-1",
                            agreement_id="ag-music", document_ref="contracts/music-v2.pdf",
                            content={"title": "音乐节赞助协议（删去排他）"}, change_note="删除排他条款")
        music_right = service.add_right(
            request_id="right-music", actor_id="legal-1", agreement_id="ag-music",
            version_no=2, event_type="music_festival", right_kind="exposure",
            title="舞台两侧品牌墙", venue="广州海心沙", region="华南",
            window_start=iso(7, 15), window_end=iso(7, 17),
            spec={"placement_slot": "舞台侧墙"}).resource_id
        music_fee = service.add_fee(request_id="fee-music", actor_id="fin-1",
                                    right_id=music_right, label="音乐节露出费",
                                    amount_minor=400_000, currency="CNY",
                                    due_kind="per_acceptance").resource_id
        service.sign_agreement(request_id="sign-music", actor_id="legal-1",
                               agreement_id="ag-music", version_no=2)

        # 3) 现场凭证：赛车露出部分验收 60%
        service.add_session(request_id="sess-race", actor_id="mkt-1",
                            source_right_id=race_right, name="上海站正赛",
                            venue="上海国际赛车场", region="华东",
                            starts_at="2026-06-10T09:00:00Z", ends_at="2026-06-10T18:00:00Z")
        ev_race = service.upload_evidence(
            request_id="ev-race", actor_id="mkt-1", right_id=race_right,
            evidence_kind="现场照片包", file_ref="s3://brand/race.zip",
            content_hash="sha256:race-evidence-001", session_id=None).resource_id
        service.decide_acceptance(request_id="acc-race", actor_id="mkt-1",
                                  evidence_id=ev_race, decision="partial",
                                  accepted_ratio=60, note="护栏只安装了六成")
        # 同一份凭证重复上传（换 request_id 与文件名）不得制造第二次验收
        duplicate = service.upload_evidence(
            request_id="ev-race-dup", actor_id="mkt-1", right_id=race_right,
            evidence_kind="现场照片包", file_ref="s3://brand/race-copy.zip",
            content_hash="sha256:race-evidence-001")
        duplicate_accepted = duplicate.resource_id == ev_race

        # 4) 音乐节现场提出争议，只冻结音乐节费用
        ev_music = service.upload_evidence(
            request_id="ev-music", actor_id="mkt-1", right_id=music_right,
            evidence_kind="现场照片", file_ref="s3://brand/music.jpg",
            content_hash="sha256:music-evidence-001").resource_id
        dispute = service.decide_acceptance(
            request_id="acc-music", actor_id="mkt-1", evidence_id=ev_music,
            decision="disputed", dispute_reason="品牌墙被主办方物料遮挡")
        dispute_id = database.connection.execute(
            "SELECT dispute_id FROM br_acceptance_events WHERE acceptance_id=?",
            (dispute.resource_id,)).fetchone()["dispute_id"]
        race_fee = database.connection.execute(
            "SELECT status, payable_minor FROM br_fee_items WHERE right_id=?",
            (race_right,)).fetchone()
        music_fee_row = database.connection.execute(
            "SELECT status, payable_minor FROM br_fee_items WHERE fee_id=?",
            (music_fee,)).fetchone()
        other_settles = race_fee["status"] == "payable" and race_fee["payable_minor"] == 600_000
        music_frozen = music_fee_row["status"] == "frozen"

        # 赛车已完成权益照常结算
        race_fee_id = database.connection.execute(
            "SELECT fee_id FROM br_fee_items WHERE right_id=?", (race_right,)).fetchone()["fee_id"]
        payment = service.propose_payment(
            request_id="pay-race", actor_id="fin-1", agreement_id="ag-race",
            amount_minor=600_000, currency="CNY", memo="上海站六成护栏露出",
            allocations=[{"fee_id": race_fee_id, "amount_minor": 600_000,
                          "evidence_id": ev_race}])
        service.complete_payment(request_id="pay-race-done", actor_id="fin-1",
                                 payment_id=payment.resource_id)
        explanation = service.get_payment_explanation(actor_id="fin-1",
                                                      payment_id=payment.resource_id)

        # 5) 争议裁决支持 50%，音乐节费用解冻
        service.resolve_dispute(request_id="resolve-music", actor_id="legal-1",
                                dispute_id=dispute_id,
                                resolution_note="确认半天遮挡，按一半结算", granted_ratio=50)
        music_fee_after = database.connection.execute(
            "SELECT status, payable_minor FROM br_fee_items WHERE fee_id=?",
            (music_fee,)).fetchone()

        # 6) 续期：复制权益文案，但旧场次、费用不延续
        renewed = service.renew_agreement(
            request_id="renew-race", actor_id="legal-1", new_agreement_id="ag-race-2027",
            source_agreement_id="ag-race", document_ref="contracts/race-2027-v1.pdf",
            content={"title": "2027 赛车赞助续期"})
        renewed_detail = service.get_agreement_detail(actor_id="legal-1",
                                                      agreement_id="ag-race-2027")

        audit_valid, audit_count = base.verify_audit()
        result = {
            "status": "ok",
            "conflicts_previewed": len(findings),
            "conflicting_sign_blocked": blocked_sign,
            "duplicate_evidence_reused": duplicate_accepted,
            "race_payable_minor": race_fee["payable_minor"],
            "other_rights_settle_during_dispute": other_settles,
            "music_fee_frozen_during_dispute": music_frozen,
            "music_fee_after_resolution": {"status": music_fee_after["status"],
                                           "payable_minor": music_fee_after["payable_minor"]},
            "payment_total_explained": explanation["total_explained_minor"],
            "payment_links_to_evidence": explanation["allocations"][0]["evidence"] is not None,
            "renewal_copied_rights": renewed.resource_id and len(renewed_detail["rights"]),
            "renewal_copied_sessions": len(renewed_detail["sessions"]),
            "audit_valid": audit_valid,
            "audit_events": audit_count,
        }
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    expected = {
        "status": "ok", "conflicts_previewed": 1, "conflicting_sign_blocked": True,
        "duplicate_evidence_reused": True, "race_payable_minor": 600_000,
        "other_rights_settle_during_dispute": True, "music_fee_frozen_during_dispute": True,
        "payment_total_explained": 600_000, "payment_links_to_evidence": True,
        "renewal_copied_sessions": 0, "audit_valid": True,
    }
    ok = all(result[k] == v for k, v in expected.items())
    ok &= result["music_fee_after_resolution"] == {"status": "payable", "payable_minor": 200_000}
    ok &= result["renewal_copied_rights"] >= 1
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
