"""获奖服务资源分配平台的端到端规则测试。"""

import unittest
from datetime import datetime, timedelta, timezone

from creative_program_foundation.allocation import (
    AllocationService,
    LINE_ABANDONED,
    LINE_EXPIRED,
    LINE_FULFILLED,
    LINE_OCCUPIED,
    LINE_RESERVED,
    LINE_WAITLISTED,
)
from creative_program_foundation.clock import FixedClock
from creative_program_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from creative_program_foundation.storage import Database


BASE = datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)


class MovableClock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)

    def move_to(self, value):
        self.value = value


class AllocationFixture(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MovableClock(BASE)
        self.service = AllocationService(self.database, self.clock)
        s = self.service
        # 分配服务要求真实操作者账号
        self.database.connection.execute(
            "INSERT OR REPLACE INTO organizations(organization_id,name,created_at) VALUES('org','机构','t')")
        for actor_id, name, role in (("admin-1", "管理员一", "admin"),
                                     ("op-2", "管理员二", "admin")):
            self.database.connection.execute(
                "INSERT INTO actors(actor_id,display_name,role,organization_id,active,created_at) "
                "VALUES(?,?,?, 'org',1,'t')", (actor_id, name, role))
        s.register_team(request_id="t1", actor_id="admin-1", team_id="team-a", name="甲团队")
        s.register_team(request_id="t2", actor_id="admin-1", team_id="team-b", name="乙团队")
        s.register_provider(request_id="p1", actor_id="admin-1", provider_id="prov-x", name="空间方")
        s.register_provider(request_id="p2", actor_id="admin-1", provider_id="prov-y", name="渠道方")
        # 团队与资源方联系人
        self.database.connection.execute(
            "INSERT INTO actors(actor_id,display_name,role,organization_id,active,created_at) "
            "VALUES('c-team-a','甲联系人','operator','org',1,'t')")
        self.database.connection.execute(
            "INSERT INTO actors(actor_id,display_name,role,organization_id,active,created_at) "
            "VALUES('c-team-b','乙联系人','operator','org',1,'t')")
        self.database.connection.execute(
            "INSERT INTO actors(actor_id,display_name,role,organization_id,active,created_at) "
            "VALUES('c-prov-x','空间方联系人','operator','org',1,'t')")
        self.database.connection.execute(
            "INSERT INTO al_contacts(actor_id,party,party_ref,created_at) VALUES('c-team-a','team','team-a','t')")
        self.database.connection.execute(
            "INSERT INTO al_contacts(actor_id,party,party_ref,created_at) VALUES('c-team-b','team','team-b','t')")
        self.database.connection.execute(
            "INSERT INTO al_contacts(actor_id,party,party_ref,created_at) VALUES('c-prov-x','provider','prov-x','t')")
        # 两个项目：A 特等+已商品化+紧急；B 一等+试产
        s.register_project(request_id="pr-a", actor_id="admin-1", project_id="proj-a",
                           team_id="team-a", title="甲项目", award_level=4, maturity_level=5, urgency=3)
        s.register_project(request_id="pr-b", actor_id="admin-1", project_id="proj-b",
                           team_id="team-b", title="乙项目", award_level=3, maturity_level=3, urgency=1)

    def tearDown(self):
        self.database.close()

    def add_space(self, resource_id="space-1", capacity=1, min_award=1, min_maturity=1,
                  materials=None, deadline_days=30, mutex_group=None, requires=None,
                  window_id="win-1", ends_days=30):
        self.service.register_resource(
            request_id="res-" + resource_id, actor_id="admin-1", resource_id=resource_id,
            service_type="space", provider_id="prov-x", title="一号空间",
            min_award_level=min_award, min_maturity=min_maturity,
            fulfillment_deadline_days=deadline_days, commitment="提供一个展位三个月",
            mutex_group=mutex_group, requires=requires,
            required_materials=materials or ["business_license"])
        self.service.add_window(
            request_id="win-" + window_id, actor_id="admin-1", window_id=window_id,
            resource_id=resource_id,
            starts_at="2026-10-01T00:00:00Z",
            ends_at=(BASE + timedelta(days=ends_days)).isoformat().replace("+00:00", "Z"),
            capacity=capacity)
        return window_id


class AllocationDecisionTest(AllocationFixture):
    def test_higher_priority_beats_first_come(self):
        # 容量只有 1：先到的 B 项目材料慢也没关系——评分高的 A 后申请仍先获配
        self.add_space(capacity=1)
        self.service.submit_application(
            request_id="app-b", actor_id="admin-1", application_id="app-b",
            project_id="proj-b", lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        self.clock.advance(hours=10)
        self.service.submit_application(
            request_id="app-a", actor_id="admin-1", application_id="app-a",
            project_id="proj-a", lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        view_a = self.service.get_application("app-a")
        view_b = self.service.get_application("app-b")
        self.assertEqual(LINE_RESERVED, view_a["lines"][0]["status"])
        self.assertEqual(LINE_WAITLISTED, view_b["lines"][0]["status"])
        self.assertEqual(1, view_b["lines"][0]["waitlist_position"])
        self.assertIn("capacity_unavailable", view_b["lines"][0]["current_blockers"])

    def test_hard_eligibility_rejection_explains_reasons(self):
        self.add_space(capacity=5, min_award=4, min_maturity=5)
        result = self.service.submit_application(
            request_id="app-b", actor_id="admin-1", application_id="app-b",
            project_id="proj-b", lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        view = result["application"]
        self.assertEqual("rejected", view["lines"][0]["status"])
        reasons = view["lines"][0]["reasons"]
        self.assertIn("award_level_insufficient", reasons)
        self.assertIn("maturity_insufficient", reasons)

    def test_reservation_holds_capacity_against_higher_priority(self):
        self.add_space(capacity=1)
        self.service.submit_application(
            request_id="app-b", actor_id="admin-1", application_id="app-b",
            project_id="proj-b", lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        # B 完成材料与双方确认并转正
        line_b = self.service.get_application("app-b")["lines"][0]
        self.service.add_material(actor_id="admin-1",
                                  application_id="app-b", line_id=line_b["line_id"],
                                  kind="business_license", document_ref="doc://b")
        self.service.confirm_line(actor_id="c-team-b", application_id="app-b",
                                  line_id=line_b["line_id"])
        self.service.confirm_line(actor_id="c-prov-x", application_id="app-b",
                                  line_id=line_b["line_id"])
        converted = self.service.convert_line(actor_id="admin-1", application_id="app-b",
                                              line_id=line_b["line_id"])
        self.assertEqual(LINE_OCCUPIED, converted["status"])
        self.service.submit_application(
            request_id="app-a", actor_id="admin-1", application_id="app-a",
            project_id="proj-a", lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        # 高优先级 A 也只能候补
        self.assertEqual(LINE_WAITLISTED,
                         self.service.get_application("app-a")["lines"][0]["status"])

    def test_expiry_releases_and_promotes_waitlist_deterministically(self):
        self.add_space(capacity=1)
        self.service.submit_application(
            request_id="app-b", actor_id="admin-1", application_id="app-b",
            project_id="proj-b", lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        self.clock.advance(hours=10)
        self.service.submit_application(
            request_id="app-a", actor_id="admin-1", application_id="app-a",
            project_id="proj-a", lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        # B 是候补，A 预留；A 在 72 小时内不补材料
        self.clock.advance(hours=73)
        counts = self.service.run_due_processing()
        self.assertEqual(1, counts["expired"])
        self.assertEqual(1, counts["promoted"])
        view_a = self.service.get_application("app-a")
        view_b = self.service.get_application("app-b")
        self.assertEqual(LINE_EXPIRED, view_a["lines"][0]["status"])
        self.assertEqual(LINE_RESERVED, view_b["lines"][0]["status"])
        # 候补预留期限从释放时刻（A 到期时）起算 72 小时
        expected = (BASE + timedelta(hours=10 + 73 + 72)).isoformat().replace("+00:00", "Z")
        self.assertEqual(expected, view_b["lines"][0]["expires_at"])
        # 重复推进幂等
        again = self.service.run_due_processing()
        self.assertEqual(0, again["expired"])

    def test_restart_keeps_due_outcomes(self):
        self.add_space(capacity=1)
        self.service.submit_application(
            request_id="app-b", actor_id="admin-1", application_id="app-b",
            project_id="proj-b", lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        self.clock.advance(hours=10)
        self.service.submit_application(
            request_id="app-a", actor_id="admin-1", application_id="app-a",
            project_id="proj-a", lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        self.clock.advance(hours=73)
        # 模拟重启：新服务实例、系统时间继续向前，但推进结果锚定历史到期时刻
        later = FixedClock(self.clock.value + timedelta(days=2))
        restarted = AllocationService(self.database, later)
        counts = restarted.run_due_processing()
        self.assertEqual(1, counts["expired"])
        self.assertEqual(1, counts["promoted"])
        view_b = restarted.get_application("app-b")
        expected = (BASE + timedelta(hours=10 + 73 + 72)).isoformat().replace("+00:00", "Z")
        self.assertEqual(expected, view_b["lines"][0]["expires_at"])
        # B 的候补预留其实也早该到期（因为“现在”又过了两天）——再推进应过期 B，且无人可推
        again = restarted.run_due_processing()
        self.assertEqual(1, again["expired"])
        self.assertEqual(LINE_EXPIRED, restarted.get_application("app-b")["lines"][0]["status"])


class LifecycleTest(AllocationFixture):
    def _occupy(self, app="app-b", project="proj-b", team_actor="c-team-b"):
        self.add_space(capacity=1)
        self.service.submit_application(
            request_id=app, actor_id="admin-1", application_id=app, project_id=project,
            lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        line = self.service.get_application(app)["lines"][0]
        self.service.add_material(actor_id="admin-1", application_id=app,
                                  line_id=line["line_id"], kind="business_license",
                                  document_ref="doc://x")
        self.service.confirm_line(actor_id=team_actor, application_id=app,
                                  line_id=line["line_id"])
        self.service.confirm_line(actor_id="c-prov-x", application_id=app,
                                  line_id=line["line_id"])
        self.service.convert_line(actor_id="admin-1", application_id=app,
                                  line_id=line["line_id"])
        return line["line_id"]

    def test_conversion_requires_materials_and_two_confirmations(self):
        self.add_space(capacity=1)
        self.service.submit_application(
            request_id="app-b", actor_id="admin-1", application_id="app-b",
            project_id="proj-b", lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        line = self.service.get_application("app-b")["lines"][0]
        with self.assertRaises(ConflictError):
            self.service.convert_line(actor_id="admin-1", application_id="app-b",
                                      line_id=line["line_id"])
        pending = self.service.get_application("app-b")["lines"][0]["pending_conversion_requirements"]
        self.assertTrue(any("business_license" in item for item in pending))
        self.assertIn("required_confirmation_missing", pending)

    def test_partial_fulfillment_is_never_reclaimed_on_expiry_style_failure(self):
        line_id = self._occupy()
        # 资源方先交付一半
        self.service.record_delivery(actor_id="c-prov-x", application_id="app-b",
                                     line_id=line_id, qty=1)
        # 运营判定项目里程碑失败：剩余 1 份释放，已交付 1 份保留
        result = self.service.mark_milestone(actor_id="admin-1", application_id="app-b",
                                             line_id=line_id, milestone_seq=1, met=False)
        self.assertEqual(1, result["released_qty"])
        view = self.service.get_application("app-b")["lines"][0]
        self.assertEqual("stage_failed", view["status"])
        self.assertEqual(1, view["fulfilled_qty"])
        # 资源方交付清单仍能看到该部分履约
        deliveries = self.service.provider_deliveries("prov-x")
        self.assertEqual(1, deliveries["count"])
        self.assertEqual(0, deliveries["items"][0]["open_qty"])

    def test_fulfilled_line_not_revoked_by_capacity_reduction(self):
        line_id = self._occupy()
        self.service.record_delivery(actor_id="c-prov-x", application_id="app-b",
                                     line_id=line_id, qty=1)
        # 全额兑现后状态为 fulfilled；缩减容量到 0 也不能回收
        self.assertEqual(LINE_FULFILLED,
                         self.service.get_application("app-b")["lines"][0]["status"])
        result = self.service.reduce_window_capacity(
            request_id="red-1", actor_id="admin-1", window_id="win-1", new_capacity=0)
        self.assertTrue(result["clamped_to_delivered"])
        self.assertEqual(1, result["effective_capacity"])
        self.assertEqual([], result["revoked"])

    def test_capacity_reduction_revokes_reservation_then_promotes(self):
        self.add_space(capacity=2)
        # A、B 各占一份预留，C 候补；缩减到 1 时撤掉优先级低的 B 预留
        self.service.submit_application(
            request_id="app-b", actor_id="admin-1", application_id="app-b",
            project_id="proj-b", lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        self.clock.advance(hours=1)
        self.service.submit_application(
            request_id="app-a", actor_id="admin-1", application_id="app-a",
            project_id="proj-a", lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        # 需要第三个项目
        self.service.register_team(request_id="t3", actor_id="admin-1", team_id="team-c",
                                   name="丙团队")
        self.service.register_project(request_id="pr-c", actor_id="admin-1", project_id="proj-c",
                                      team_id="team-c", title="丙项目", award_level=2,
                                      maturity_level=2, urgency=0)
        self.clock.advance(hours=1)
        self.service.submit_application(
            request_id="app-c", actor_id="admin-1", application_id="app-c",
            project_id="proj-c", lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        result = self.service.reduce_window_capacity(
            request_id="red-1", actor_id="admin-1", window_id="win-1", new_capacity=1)
        revoked_ids = [item["line_id"] for item in result["revoked"]]
        line_b = self.service.get_application("app-b")["lines"][0]["line_id"]
        self.assertIn(line_b, revoked_ids)
        self.assertEqual("capacity_reduced",
                         self.service.get_application("app-b")["lines"][0]["status"])
        # A 仍预留
        self.assertEqual(LINE_RESERVED,
                         self.service.get_application("app-a")["lines"][0]["status"])
        # C 仍然候补（容量 1 全被 A 占）
        self.assertEqual(LINE_WAITLISTED,
                         self.service.get_application("app-c")["lines"][0]["status"])

    def test_abandon_promotes_waitlist(self):
        self.add_space(capacity=1)
        self.service.submit_application(
            request_id="app-b", actor_id="admin-1", application_id="app-b",
            project_id="proj-b", lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        self.clock.advance(hours=1)
        self.service.submit_application(
            request_id="app-a", actor_id="admin-1", application_id="app-a",
            project_id="proj-a", lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        line_a = self.service.get_application("app-a")["lines"][0]
        result = self.service.abandon_line(actor_id="c-team-a", application_id="app-a",
                                           line_id=line_a["line_id"])
        self.assertEqual(1, result["released_qty"])
        self.assertEqual(1, len(result["promoted"]))
        self.assertEqual(LINE_RESERVED,
                         self.service.get_application("app-b")["lines"][0]["status"])
        self.assertEqual(LINE_ABANDONED,
                         self.service.get_application("app-a")["lines"][0]["status"])


class ConflictTest(AllocationFixture):
    def test_mutex_and_duplicate_blocked_across_controller_group(self):
        # 同一实际控制团队的两个马甲团队
        self.service.register_team(request_id="t-a2", actor_id="admin-1", team_id="team-a2",
                                   name="甲马甲", controller_group_id="team-a")
        self.service.register_project(request_id="pr-a2", actor_id="admin-1", project_id="proj-a2",
                                      team_id="team-a2", title="马甲项目", award_level=4,
                                      maturity_level=5, urgency=3)
        self.service.register_resource(
            request_id="res-space-2", actor_id="admin-1", resource_id="space-2",
            service_type="space", provider_id="prov-x", title="二号空间",
            min_award_level=1, min_maturity=1, fulfillment_deadline_days=30,
            commitment="互斥空间", mutex_group="downtown")
        self.service.register_resource(
            request_id="res-space-1", actor_id="admin-1", resource_id="space-1",
            service_type="space", provider_id="prov-x", title="一号空间",
            min_award_level=1, min_maturity=1, fulfillment_deadline_days=30,
            commitment="互斥空间", mutex_group="downtown")
        for rid, wid in (("space-1", "w1"), ("space-2", "w2")):
            self.service.add_window(
                request_id="w-" + wid, actor_id="admin-1", window_id=wid, resource_id=rid,
                starts_at="2026-10-01T00:00:00Z", ends_at="2026-11-30T00:00:00Z", capacity=1)
        # 甲团队先占 space-1
        self.service.submit_application(
            request_id="app-a", actor_id="admin-1", application_id="app-a",
            project_id="proj-a",
            lines=[{"resource_id": "space-1", "window_id": "w1"}])
        # 马甲团队申请互斥的 space-2：被拒
        result = self.service.submit_application(
            request_id="app-a2", actor_id="admin-1", application_id="app-a2",
            project_id="proj-a2",
            lines=[{"resource_id": "space-2", "window_id": "w2"}])
        reasons = result["application"]["lines"][0]["reasons"]
        self.assertTrue(any(r.startswith("mutex_conflict"), reasons))
        # 同一资源重复占位同样被拒
        result2 = self.service.submit_application(
            request_id="app-a3", actor_id="admin-1", application_id="app-a3",
            project_id="proj-a2",
            lines=[{"resource_id": "space-1", "window_id": "w1"}])
        self.assertTrue(any(r.startswith("duplicate_holding"),
                            result2["application"]["lines"][0]["reasons"]))

    def test_dependency_waits_then_unlocks(self):
        # 渠道上架依赖宣传：宣传容量 1，渠道容量 1
        self.service.register_resource(
            request_id="res-pub", actor_id="admin-1", resource_id="pub-1",
            service_type="publicity", provider_id="prov-y", title="宣传包",
            min_award_level=1, min_maturity=1, fulfillment_deadline_days=30,
            commitment="一次专题宣传", required_materials=[])
        self.service.add_window(request_id="w-pub", actor_id="admin-1", window_id="w-pub",
                                resource_id="pub-1", starts_at="2026-10-01T00:00:00Z",
                                ends_at="2026-12-31T00:00:00Z", capacity=1)
        self.service.register_resource(
            request_id="res-ch", actor_id="admin-1", resource_id="ch-1",
            service_type="channel", provider_id="prov-y", title="渠道位",
            min_award_level=1, min_maturity=1, fulfillment_deadline_days=30,
            commitment="上架渠道", required_materials=[], requires=["pub-1"])
        self.service.add_window(request_id="w-ch", actor_id="admin-1", window_id="w-ch",
                                resource_id="ch-1", starts_at="2026-10-01T00:00:00Z",
                                ends_at="2026-12-31T00:00:00Z", capacity=1)
        result = self.service.submit_application(
            request_id="app-b", actor_id="admin-1", application_id="app-b",
            project_id="proj-b",
            lines=[{"resource_id": "ch-1", "window_id": "w-ch"}])
        line = result["application"]["lines"][0]
        self.assertEqual(LINE_WAITLISTED, line["status"])
        self.assertTrue(any("dependency_unmet" in r for r in line["reasons"]))
        # 再提交一个组合：宣传+渠道，宣传获预留，渠道仍候补（宣传尚未兑现）
        result2 = self.service.submit_application(
            request_id="app-a", actor_id="admin-1", application_id="app-a",
            project_id="proj-a",
            lines=[{"resource_id": "pub-1", "window_id": "w-pub"},
                   {"resource_id": "ch-1", "window_id": "w-ch"}])
        statuses = {item["resource_id"]: item["status"] for item in result2["application"]["lines"]}
        self.assertEqual(LINE_RESERVED, statuses["pub-1"])
        self.assertEqual(LINE_WAITLISTED, statuses["ch-1"])


class OverrideTest(AllocationFixture):
    def test_override_requires_two_distinct_operators_and_records_impact(self):
        self.add_space(capacity=1)
        self.service.submit_application(
            request_id="app-b", actor_id="admin-1", application_id="app-b",
            project_id="proj-b", lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        self.clock.advance(hours=1)
        self.service.submit_application(
            request_id="app-a", actor_id="admin-1", application_id="app-a",
            project_id="proj-a", lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        line_b = self.service.get_application("app-b")["lines"][0]
        proposed = self.service.propose_override(
            request_id="ov-1", actor_id="admin-1", application_id="app-b",
            line_id=line_b["line_id"], reason="重点扶持")
        # 提出人自己不能批准
        with self.assertRaises(PermissionDenied):
            self.service.decide_override(actor_id="admin-1",
                                         override_id=proposed["override_id"],
                                         approve=True)
        result = self.service.decide_override(actor_id="op-2",
                                              override_id=proposed["override_id"], approve=True)
        impact = result["impact"]
        # 跳过的候补数为 0（A 是预留而非候补），但记录了超配容量
        self.assertEqual(1, impact["overcommit_qty"])
        self.assertEqual("team-b", impact["beneficiary_controller_group"])
        self.assertEqual(LINE_RESERVED,
                         self.service.get_application("app-b")["lines"][0]["status"])

    def test_override_cannot_bypass_mutex(self):
        self.service.register_team(request_id="t-a2", actor_id="admin-1", team_id="team-a2",
                                   name="甲马甲", controller_group_id="team-a")
        self.service.register_project(request_id="pr-a2", actor_id="admin-1", project_id="proj-a2",
                                      team_id="team-a2", title="马甲项目", award_level=4,
                                      maturity_level=5, urgency=3)
        self.service.register_resource(
            request_id="res-s1", actor_id="admin-1", resource_id="s1", service_type="space",
            provider_id="prov-x", title="空间一", min_award_level=1, min_maturity=1,
            fulfillment_deadline_days=30, commitment="x", mutex_group="g")
        self.service.register_resource(
            request_id="res-s2", actor_id="admin-1", resource_id="s2", service_type="space",
            provider_id="prov-x", title="空间二", min_award_level=1, min_maturity=1,
            fulfillment_deadline_days=30, commitment="x", mutex_group="g")
        for rid, wid in (("s1", "g1"), ("s2", "g2")):
            self.service.add_window(request_id="w-" + wid, actor_id="admin-1",
                                    window_id=wid, resource_id=rid,
                                    starts_at="2026-10-01T00:00:00Z",
                                    ends_at="2026-12-31T00:00:00Z", capacity=1)
        self.service.submit_application(
            request_id="app-a", actor_id="admin-1", application_id="app-a",
            project_id="proj-a", lines=[{"resource_id": "s1", "window_id": "g1"}])
        rejected = self.service.submit_application(
            request_id="app-a2", actor_id="admin-1", application_id="app-a2",
            project_id="proj-a2", lines=[{"resource_id": "s2", "window_id": "g2"}])
        line = rejected["application"]["lines"][0]
        proposed = self.service.propose_override(
            request_id="ov-x", actor_id="admin-1", application_id="app-a2",
            line_id=line["line_id"], reason="试试绕过")
        with self.assertRaises(PermissionDenied):
            self.service.decide_override(actor_id="op-2",
                                         override_id=proposed["override_id"], approve=True)


class PolicyTest(AllocationFixture):
    def test_policy_versions_freeze_and_compare_without_rewriting(self):
        self.add_space(capacity=1)
        self.service.submit_application(
            request_id="app-b", actor_id="admin-1", application_id="app-b",
            project_id="proj-b", lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        self.clock.advance(hours=1)
        self.service.submit_application(
            request_id="app-a", actor_id="admin-1", application_id="app-a",
            project_id="proj-a", lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        # 旧政策 v1 下 A 预留、B 候补
        self.assertEqual("v1", self.service.get_application("app-a")["policy_version"])
        # 新政策极端偏好紧急度；B 已冻结的分数不变
        self.service.create_policy(
            request_id="pol-v2", actor_id="admin-1", policy_version="v2", label="紧急优先",
            weights={"award": 0.0, "maturity": 0.0, "urgency": 100.0},
            reservation_ttl_hours=48, required_confirmations=2)
        self.service.register_team(request_id="t-c", actor_id="admin-1",
                                   team_id="team-c", name="丙")
        self.service.register_project(request_id="pr-c", actor_id="admin-1", project_id="proj-c",
                                      team_id="team-c", title="丙项目", award_level=1,
                                      maturity_level=1, urgency=3)
        self.service.submit_application(
            request_id="app-c", actor_id="admin-1", application_id="app-c",
            project_id="proj-c", lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        view_b = self.service.get_application("app-b")
        view_c = self.service.get_application("app-c")
        self.assertEqual("v1", view_b["policy_version"])
        self.assertEqual("v2", view_c["policy_version"])
        # v2 下 C 的分数（300）虽高，但队列里 B 的冻结分数仍按 v1 计算：
        # B = 10*3+6*3+4*1=52；二者在不同政策下，排序仍按各自冻结分数
        self.assertGreater(view_b["lines"][0]["priority_score"], 0)
        comparison = self.service.policy_comparison()
        versions = {item["policy_version"]: item for item in comparison["versions"]}
        self.assertIn("v1", versions)
        self.assertIn("v2", versions)
        self.assertGreaterEqual(versions["v1"]["line_counts"].get("waitlisted", 0), 1)
        self.assertGreaterEqual(versions["v2"]["line_counts"].get("waitlisted", 0), 1)
        # 历史审计链仍然完整
        valid, _ = self.service.database.connection.execute(
            "SELECT 1").fetchone() and __import__(
            "creative_program_foundation.audit", fromlist=["verify_chain"]).verify_chain(
            self.service.database.connection)
        self.assertTrue(valid)

    def test_idempotent_replay(self):
        self.add_space(capacity=1)
        payload = dict(request_id="app-b", actor_id="admin-1", application_id="app-b",
                       project_id="proj-b",
                       lines=[{"resource_id": "space-1", "window_id": "win-1"}])
        first = self.service.submit_application(**payload)
        second = self.service.submit_application(**payload)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["resource_id"], second["resource_id"])


if __name__ == "__main__":
    unittest.main()
