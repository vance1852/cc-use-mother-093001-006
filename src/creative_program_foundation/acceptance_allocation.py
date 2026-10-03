"""获奖服务资源分配平台的离线端到端验收。

在临时 SQLite 数据库中走通：团队关联与实际控制组、获奖等级/成熟度打分、
组合申请、限时预留、材料与多方确认、稳定候补、逾期推进、先后依赖、
部分履约、里程碑失败、提供方容量调整、双人特批与公平性量化、政策版本比较，
以及重启后预留到期与候补推进的一致性。

关键时序：先提交的低优先级团队只能获得“限时预留”，逾期未完成材料即释放；
高优先级且已商品化的团队随后凭稳定候补顺序获配，而不是被先到先得永久挡在门外。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .allocation import AllocationService
from .clock import FixedClock
from .errors import PermissionDenied
from .storage import Database


START = datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)


def run() -> dict[str, object]:
    """执行完整分配链并返回可断言结果。"""

    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "allocation_acceptance.sqlite3"
        clock = FixedClock(START)
        database = Database(db_path)
        service = AllocationService(database, clock)
        checks: dict[str, bool] = {}

        # ---- 主体与角色
        service.register_organization(request_id="org", actor_id="bootstrap",
                                      organization_id="org-001", name="文创赛事运营方")
        for rid, actor_id, name, role in [
                ("a-admin", "admin-001", "系统管理员", "admin"),
                ("a-op", "operator-001", "赛事运营", "operator"),
                ("a-alpha", "alpha-contact", "甲队联系人", "applicant"),
                ("a-bravo", "bravo-contact", "乙队联系人", "applicant"),
                ("a-charlie", "charlie-contact", "丙队联系人", "applicant"),
                ("a-delta", "delta-contact", "丁队联系人", "applicant"),
                ("a-space", "space-contact", "空间提供方联系人", "provider"),
                ("a-channel", "channel-contact", "渠道提供方联系人", "provider")]:
            service.register_actor(
                request_id=rid, actor_id="bootstrap" if actor_id == "admin-001" else "admin-001",
                new_actor_id=actor_id, display_name=name, role=role, organization_id="org-001")
        for rid, team_id, name, contact in [
                ("t-alpha", "team-alpha", "甲团队", "alpha-contact"),
                ("t-bravo", "team-bravo", "乙团队", "bravo-contact"),
                ("t-charlie", "team-charlie", "丙团队", "charlie-contact"),
                ("t-delta", "team-delta", "丁团队", "delta-contact")]:
            service.register_team(request_id=rid, actor_id="admin-001", team_id=team_id,
                                  name=name, contact_actor_id=contact)
        # 乙、丙受同一实际控制人控制
        service.relate_teams(request_id="rel-bc", actor_id="admin-001",
                             team_id="team-bravo", other_team_id="team-charlie")
        checks["control_group_detected"] = service.get_team("team-charlie").control_group == "team-bravo"

        service.register_provider(request_id="p-space", actor_id="admin-001", provider_id="space-co",
                                  name="城市空间运营商", contact_actor_id="space-contact")
        service.register_provider(request_id="p-channel", actor_id="admin-001", provider_id="channel-co",
                                  name="线上渠道平台", contact_actor_id="channel-contact")
        for rid, award_id, team, level, title, maturity in [
                ("aw-alpha", "award-alpha", "team-alpha", "grand", "可商品化沉浸装置", 92),
                ("aw-bravo", "award-bravo", "team-bravo", "second", "概念设计稿", 58),
                ("aw-charlie", "award-charlie", "team-charlie", "excellence", "同一控制人的另一作品", 55),
                ("aw-delta", "award-delta", "team-delta", "first", "丁队成熟项目", 85)]:
            service.register_award(request_id=rid, actor_id="admin-001", award_id=award_id,
                                   team_id=team, award_level=level, title=title, maturity_score=maturity)

        # ---- 资源与时段
        service.register_resource(
            request_id="r-space", actor_id="admin-001", resource_id="space-hall",
            provider_id="space-co", category="space", name="一号展厅档期",
            materials=["settlement"], parties=["team", "provider"],
            milestones=[{"code": "open", "due_days": 30}], fulfillment_due_days=15)
        service.register_window(request_id="w-space", actor_id="admin-001", window_id="space-oct",
                                resource_id="space-hall", starts_at="2026-10-01T00:00:00Z",
                                ends_at="2026-11-30T00:00:00Z", capacity=1, note="十月档期")
        # 渠道上架依赖空间获配
        service.register_resource(
            request_id="r-channel", actor_id="admin-001", resource_id="channel-listing",
            provider_id="channel-co", category="channel", name="首页渠道位",
            requires=["space-hall"], materials=["contract"], parties=["team", "provider"])
        service.register_window(request_id="w-channel", actor_id="admin-001", window_id="channel-q4",
                                resource_id="channel-listing", starts_at="2026-10-01T00:00:00Z",
                                ends_at="2026-12-31T00:00:00Z", capacity=1)

        # ---- 乙先到先得形成限时预留；甲等级高、成熟且紧急，进入稳定候补第一位
        bravo = service.submit_application(
            request_id="app-bravo", actor_id="bravo-contact", award_id="award-bravo",
            items=[{"resource_id": "space-hall", "window_id": "space-oct", "urgency": 10},
                   {"resource_id": "channel-listing", "window_id": "channel-q4", "urgency": 10}])
        alpha = service.submit_application(
            request_id="app-alpha", actor_id="alpha-contact", award_id="award-alpha",
            items=[{"resource_id": "space-hall", "window_id": "space-oct", "urgency": 90},
                   {"resource_id": "channel-listing", "window_id": "channel-q4", "urgency": 90}])
        charlie = service.submit_application(
            request_id="app-charlie", actor_id="charlie-contact", award_id="award-charlie",
            items=[{"resource_id": "space-hall", "window_id": "space-oct", "urgency": 50}])

        alpha_space = _item(service, alpha.resource_id, lambda i: i["resource_id"] == "space-hall")
        bravo_space = _item(service, bravo.resource_id, lambda i: i["resource_id"] == "space-hall")
        charlie_space = _item(service, charlie.resource_id, lambda i: True)
        checks["bravo_gets_timed_reservation"] = bravo_space["state"] == "reserved"
        checks["alpha_waitlisted_rank1"] = alpha_space["state"] == "waitlisted" \
            and alpha_space["rank"] == 1
        checks["alpha_outscores_bravo"] = alpha_space["score"] > bravo_space["score"]
        # 丙与乙同属一个实际控制组，乙持有空间时丙被同组上限拦截
        checks["charlie_blocked_by_group_cap"] = \
            charlie_space["reasons"][0]["code"] == "group_cap_pending"

        # ---- 乙材料迟迟不补，预留逾期；甲按稳定候补顺序获配，渠道随前置预留一并获配
        _advance(clock, days=3)
        sweep1 = service.sweep_due(actor_id="operator-001")
        bravo_space = _item(service, bravo.resource_id, lambda i: i["resource_id"] == "space-hall")
        alpha_space = _item(service, alpha.resource_id, lambda i: i["resource_id"] == "space-hall")
        alpha_channel = _item(service, alpha.resource_id,
                              lambda i: i["resource_id"] == "channel-listing")
        checks["bravo_reservation_expired"] = bravo_space["state"] == "expired" \
            and bravo_space["item_id"] in sweep1["expired"]
        checks["alpha_advanced_after_expiry"] = alpha_space["state"] == "reserved"
        checks["channel_waits_for_space_confirmation"] = alpha_channel["state"] == "reserved" \
            and any(r["code"] == "advanced_from_waitlist" for r in alpha_channel["reasons"])

        # 丁（高成熟度、高等级，与各方均无关联）在此刻进入候补，排在丙之前
        delta = service.submit_application(
            request_id="app-delta", actor_id="delta-contact", award_id="award-delta",
            items=[{"resource_id": "space-hall", "window_id": "space-oct", "urgency": 60}])
        delta_space = _item(service, delta.resource_id, lambda i: True)
        checks["delta_waitlisted_rank1"] = delta_space["state"] == "waitlisted" \
            and delta_space["rank"] == 1

        # ---- 甲补齐材料并经双方确认，正式占用；渠道在空间确认后随之确认
        service.supply_material(actor_id="alpha-contact", item_id=alpha_space["item_id"],
                                code="settlement", content={"doc": "settlement-v1"})
        service.confirm_party(actor_id="alpha-contact", item_id=alpha_space["item_id"], party="team")
        service.confirm_party(actor_id="space-contact", item_id=alpha_space["item_id"], party="provider")
        service.supply_material(actor_id="alpha-contact", item_id=alpha_channel["item_id"],
                                code="contract", content={"doc": "contract-v1"})
        service.confirm_party(actor_id="alpha-contact", item_id=alpha_channel["item_id"], party="team")
        service.confirm_party(actor_id="channel-contact", item_id=alpha_channel["item_id"], party="provider")
        alpha_space = _item(service, alpha.resource_id, lambda i: i["resource_id"] == "space-hall")
        alpha_channel = _item(service, alpha.resource_id,
                              lambda i: i["resource_id"] == "channel-listing")
        checks["space_confirmed"] = alpha_space["state"] == "confirmed"
        checks["channel_confirmed_after_prerequisite"] = alpha_channel["state"] == "confirmed"

        # ---- 提供方交付清单只包含自己的资源，且带履约期限
        space_deliveries = service.provider_deliveries(actor_id="space-contact")
        checks["provider_delivery_scoped"] = \
            {i["resource_id"] for i in space_deliveries["items"]} == {"space-hall"}
        checks["delivery_has_deadline"] = space_deliveries["items"][0]["deliver_by"] is not None

        # ---- 部分履约后里程碑失败：已兑现的一单位不得回收
        service.record_fulfillment(request_id="ful-1", actor_id="space-contact",
                                   item_id=alpha_space["item_id"], quantity=1, note="首周已入驻")
        milestone = service.record_milestone(actor_id="operator-001",
                                             item_id=alpha_space["item_id"], code="open",
                                             passed=False, note="未按时对公众开放")
        alpha_space = _item(service, alpha.resource_id, lambda i: i["resource_id"] == "space-hall")
        checks["fulfilled_never_reclaimed"] = alpha_space["state"] == "fulfilled" \
            and alpha_space["fulfilled_qty"] == 1 and "advanced" not in milestone

        # ---- 双人特批：为丙提议强插，必须由另一人批准，并量化越过丁的影响
        charlie_space = _item(service, charlie.resource_id, lambda i: True)
        proposal = service.propose_override(actor_id="operator-001",
                                            item_id=charlie_space["item_id"], reason="区级重点转化项目")
        try:
            service.approve_override(actor_id="operator-001", proposal_id=proposal["proposal_id"])
            checks["self_approval_blocked"] = False
        except PermissionDenied:
            checks["self_approval_blocked"] = True
        approved = service.approve_override(actor_id="admin-001",
                                            proposal_id=proposal["proposal_id"])
        checks["override_dual_approved"] = approved["status"] == "approved_pending_capacity"
        checks["fairness_quantified"] = approved["fairness"]["jumped_count"] == 1 \
            and approved["fairness"]["jumped_item_ids"] == [delta_space["item_id"]] \
            and approved["fairness"]["score_gap"] > 0 \
            and approved["fairness"]["policy_version"] == "v1"

        # ---- 提供方加档：特批的丙越过丁获配；容量再度占满，丁继续候补
        service.adjust_capacity(request_id="cap-add", actor_id="space-contact", window_id="space-oct",
                                delta=1, reason="开放二号展位作为替代档期")
        charlie_space = _item(service, charlie.resource_id, lambda i: True)
        delta_space = _item(service, delta.resource_id, lambda i: True)
        checks["override_jumps_queue"] = charlie_space["state"] == "reserved" \
            and any(r["code"] == "override_granted" for r in charlie_space["reasons"])
        checks["delta_still_waitlisted"] = delta_space["state"] == "waitlisted"

        # ---- 丙逾期不补材料，丁按稳定顺序获配
        _advance(clock, days=3)
        sweep2 = service.sweep_due(actor_id="operator-001")
        charlie_space = _item(service, charlie.resource_id, lambda i: True)
        delta_space = _item(service, delta.resource_id, lambda i: True)
        checks["charlie_expired"] = charlie_space["state"] == "expired" \
            and charlie_space["item_id"] in sweep2["expired"]
        checks["delta_advanced_after_charlie_expiry"] = delta_space["state"] == "reserved"

        # ---- 重启：状态持久化，重复扫描不产生新结果
        database.close()
        database = Database(db_path)
        service = AllocationService(database, clock)
        sweep_restart = service.sweep_due(actor_id="operator-001")
        checks["restart_sweep_idempotent"] = sweep_restart["expired"] == [] \
            and sweep_restart["advanced"] == []
        charlie_space = _item(service, charlie.resource_id, lambda i: True)
        delta_space = _item(service, delta.resource_id, lambda i: True)
        checks["restart_state_persisted"] = charlie_space["state"] == "expired" \
            and delta_space["state"] == "reserved"

        # ---- 丁也逾期不补材料
        _advance(clock, days=3)
        sweep3 = service.sweep_due(actor_id="operator-001")
        delta_space = _item(service, delta.resource_id, lambda i: True)
        checks["delta_expired_after_own_ttl"] = delta_space["state"] == "expired" \
            and delta_space["item_id"] in sweep3["expired"]

        # ---- 新政策版本只用于比较，不改写既往决策
        original = _item(service, alpha.resource_id, lambda i: i["resource_id"] == "space-hall")
        service.create_policy(request_id="policy-v2", actor_id="admin-001", rules={
            "weights": {"award_level": 20, "maturity": 70, "urgency": 10}})
        current = _item(service, alpha.resource_id, lambda i: i["resource_id"] == "space-hall")
        simulation = service.simulate_policy(actor_id="admin-001", item_id=original["item_id"],
                                             policy_version="v2")
        checks["past_decisions_not_rewritten"] = current["policy_version"] == "v1" \
            and current["score"] == original["score"]
        checks["simulation_compares_versions"] = simulation["score_delta"] != 0 \
            and simulation["actual"]["policy_version"] == "v1"

        valid, event_count = service.verify_audit()
        database.close()
        return {"status": "ok" if all(checks.values()) and valid else "failed",
                "checks": checks, "audit_events": event_count, "audit_valid": valid}


def _advance(clock: FixedClock, **kwargs) -> None:
    clock._value = (clock.now() + timedelta(**kwargs)).astimezone(timezone.utc)


def _item(service, application_id: str, match) -> dict:
    app = service.get_application(application_id=application_id, actor_id="admin-001")
    return next(item for item in app["items"] if match(item))


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
