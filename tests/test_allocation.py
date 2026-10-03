import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from creative_program_foundation.allocation import AllocationService
from creative_program_foundation.clock import ManualClock
from creative_program_foundation.errors import ConflictError, PermissionDenied, ValidationError
from creative_program_foundation.storage import Database


T0 = datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)


@contextmanager
def _closing_db(path):
    database = Database(path)
    try:
        yield database
    finally:
        database.close()


def _backup(database: Database, path: str) -> None:
    target = sqlite3.connect(path)
    try:
        database.connection.backup(target)
    finally:
        target.close()


class AllocationCase(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock(T0)
        self.database = Database(":memory:")
        self.service = AllocationService(self.database, self.clock)
        self._seed()

    def _seed(self):
        service = self.service
        service.register_organization(request_id="org", actor_id="bootstrap",
                                      organization_id="o1", name="赛事运营方")
        actors = [
            ("admin", "管理员", "admin"), ("op1", "运营", "operator"),
            ("t1c", "甲联系人", "applicant"), ("t2c", "乙联系人", "applicant"),
            ("t3c", "丙联系人", "applicant"), ("t4c", "丁联系人", "applicant"),
            ("p1c", "空间提供方", "provider"), ("p2c", "渠道提供方", "provider"),
            ("au1", "审计员", "auditor"),
        ]
        for actor_id, name, role in actors:
            service.register_actor(request_id=f"actor-{actor_id}",
                                   actor_id="bootstrap" if actor_id == "admin" else "admin",
                                   new_actor_id=actor_id, display_name=name, role=role,
                                   organization_id="o1")
        for tid, contact, name in [("t1", "t1c", "甲队"), ("t2", "t2c", "乙队"),
                                   ("t3", "t3c", "丙队"), ("t4", "t4c", "丁队")]:
            service.register_team(request_id=f"team-{tid}", actor_id="admin", team_id=tid,
                                  name=name, contact_actor_id=contact)
        service.register_provider(request_id="prov1", actor_id="admin", provider_id="p1",
                                  name="空间商", contact_actor_id="p1c")
        service.register_provider(request_id="prov2", actor_id="admin", provider_id="p2",
                                  name="渠道商", contact_actor_id="p2c")
        awards = [("aw1", "t1", "grand", "甲作品", 90), ("aw2", "t2", "second", "乙作品", 60),
                  ("aw3", "t3", "excellence", "丙作品", 55), ("aw4", "t4", "first", "丁作品", 80)]
        for award_id, team, level, title, maturity in awards:
            service.register_award(request_id=f"award-{award_id}", actor_id="admin", award_id=award_id,
                                   team_id=team, award_level=level, title=title, maturity_score=maturity)

    def resource(self, *, resource_id="sp1", provider="p1", category="space", name="展厅",
                 min_level="excellence", min_maturity=0, mutex_group=None, requires=None,
                 materials=None, parties=None, milestones=None, fulfillment_due_days=None):
        self.service.register_resource(
            request_id=f"res-{resource_id}", actor_id="admin", resource_id=resource_id,
            provider_id=provider, category=category, name=name, min_level=min_level,
            min_maturity=min_maturity, mutex_group=mutex_group, requires=requires,
            materials=materials, parties=parties, milestones=milestones,
            fulfillment_due_days=fulfillment_due_days)

    def window(self, *, window_id="w1", resource_id="sp1", capacity=1,
               starts="2026-10-01T00:00:00Z", ends="2026-11-30T00:00:00Z"):
        self.service.register_window(request_id=f"win-{window_id}", actor_id="admin",
                                     window_id=window_id, resource_id=resource_id,
                                     starts_at=starts, ends_at=ends, capacity=capacity)

    def apply(self, request_id, *, actor="t1c", award="aw1", lines=None):
        receipt = self.service.submit_application(
            request_id=request_id, actor_id=actor, award_id=award,
            items=lines or [{"resource_id": "sp1", "window_id": "w1", "urgency": 80}])
        return self.service.get_application(application_id=receipt.resource_id, actor_id=actor)

    def item(self, application, index=0):
        return application["items"][index]

    def view(self, item_id, actor="admin"):
        row = self.service.database.connection.execute(
            "SELECT application_id FROM alloc_items WHERE item_id=?", (item_id,)).fetchone()
        app = self.service.get_application(application_id=row["application_id"], actor_id=actor)
        return next(i for i in app["items"] if i["item_id"] == item_id)

    def complete_item(self, item_id, *, team="t1c", provider="p1c"):
        view = self.view(item_id)
        for code in view["required_materials"]:
            self.service.supply_material(actor_id=team, item_id=item_id, code=code, content={"v": 1})
        for party in view["required_parties"]:
            actor = {"team": team, "provider": provider, "operator": "op1"}[party]
            self.service.confirm_party(actor_id=actor, item_id=item_id, party=party)
        return self.view(item_id)

    def tearDown(self):
        self.database.close()


class DecisionTest(AllocationCase):
    def test_higher_level_and_maturity_wins_capacity(self):
        self.resource(materials=["license"])
        self.window()
        a1 = self.apply("q1")
        a2 = self.apply("q2", actor="t2c", award="aw2",
                        lines=[{"resource_id": "sp1", "window_id": "w1", "urgency": 90}])
        self.assertEqual("reserved", self.item(a1)["state"])
        self.assertEqual("waitlisted", self.item(a2)["state"])
        self.assertGreater(self.item(a1)["score"], self.item(a2)["score"])
        self.assertEqual("insufficient_capacity", self.item(a2)["reasons"][0]["code"])
        self.assertEqual(1, self.item(a2)["rank"])

    def test_hard_ineligibility_is_rejected_not_waitlisted(self):
        self.resource(min_level="grand", min_maturity=80)
        self.window()
        a = self.apply("q1", actor="t2c", award="aw2")
        self.assertEqual("rejected", self.item(a)["state"])
        self.assertEqual("ineligible_award_level", self.item(a)["reasons"][0]["code"])

    def test_materials_and_two_party_confirmations_confirm(self):
        self.resource(materials=["license", "plan"], parties=["team", "provider"])
        self.window()
        a = self.apply("q1")
        item_id = self.item(a)["item_id"]
        self.service.supply_material(actor_id="t1c", item_id=item_id, code="license", content={"v": 1})
        self.service.confirm_party(actor_id="t1c", item_id=item_id, party="team")
        self.assertEqual("reserved", self.view(item_id)["state"])
        with self.assertRaises(PermissionDenied):
            self.service.confirm_party(actor_id="t2c", item_id=item_id, party="provider")
        self.service.supply_material(actor_id="t1c", item_id=item_id, code="plan", content={"v": 2})
        self.service.confirm_party(actor_id="p1c", item_id=item_id, party="provider")
        self.assertEqual("confirmed", self.view(item_id)["state"])
        self.assertIsNone(self.view(item_id)["expires_at"])

    def test_reservation_expiry_releases_and_advances_stable_waitlist(self):
        self.resource(materials=["license"])
        self.window()
        a1 = self.apply("q1")
        a2 = self.apply("q2", actor="t2c", award="aw2")
        self.clock.advance(days=3)
        result = self.service.sweep_due(actor_id="op1")
        self.assertEqual([self.item(a1)["item_id"]], result["expired"])
        self.assertEqual("expired", self.view(self.item(a1)["item_id"])["state"])
        advanced = self.view(self.item(a2)["item_id"])
        self.assertEqual("reserved", advanced["state"])
        self.assertEqual("advanced_from_waitlist", advanced["reasons"][-1]["code"])

    def test_expiry_and_advance_are_consistent_after_restart(self):
        self.resource(materials=["license"])
        self.window()
        a1 = self.apply("q1")
        a2 = self.apply("q2", actor="t2c", award="aw2")
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "restart.sqlite3")
            _backup(self.database, path)
            self.clock.advance(days=3)
            with _closing_db(path) as restarted:
                service = AllocationService(restarted, self.clock)
                result = service.sweep_due(actor_id="op1")
                self.assertEqual([self.item(a1)["item_id"]], result["expired"])
                app2 = service.get_application(application_id=a2["application_id"], actor_id="t2c")
                self.assertEqual("reserved", self.item(app2)["state"])
            with _closing_db(path) as again_db:
                again = AllocationService(again_db, self.clock)
                second = again.sweep_due(actor_id="op1")
                self.assertEqual([], second["expired"])
                self.assertEqual([], second["advanced"])
                valid, _ = again.verify_audit()
                self.assertTrue(valid)

    def test_abandon_releases_and_promotes_next(self):
        self.resource()
        self.window()
        a1 = self.apply("q1")
        a2 = self.apply("q2", actor="t2c", award="aw2")
        receipt = self.service.abandon(request_id="ab1", actor_id="t1c",
                                       item_id=self.item(a1)["item_id"])
        self.assertFalse(receipt.replayed)
        self.assertEqual("abandoned", self.view(self.item(a1)["item_id"])["state"])
        self.assertEqual("reserved", self.view(self.item(a2)["item_id"])["state"])

    def test_milestone_failure_keeps_fulfilled_and_releases_remainder(self):
        self.service.create_policy(request_id="pol-cap", actor_id="admin", rules={
            "group_caps": {"space": 5}})
        self.resource(materials=["license"], milestones=[{"code": "m1", "due_days": 30}])
        self.window(capacity=3)
        a = self.apply("q1",
                       lines=[{"resource_id": "sp1", "window_id": "w1", "quantity": 2, "urgency": 50}])
        item_id = self.item(a)["item_id"]
        self.complete_item(item_id)
        self.service.record_fulfillment(request_id="f1", actor_id="p1c", item_id=item_id, quantity=1)
        result = self.service.record_milestone(actor_id="op1", item_id=item_id, code="m1", passed=False)
        self.assertEqual([], result["advanced"])
        view = self.view(item_id)
        self.assertEqual("released", view["state"])
        self.assertEqual(1, view["fulfilled_qty"])
        self.assertEqual(1, view["released_qty"])
        self.assertEqual(0, view["remaining_qty"])
        # 已履约的 1 个单位仍占用容量
        window = self.service.get_window("w1")
        self.assertEqual(1, window.consumed)

    def test_capacity_reduction_displaces_unfulfilled_and_protects_fulfilled(self):
        self.resource(materials=["license"], parties=["team", "provider"])
        self.window(capacity=2)
        a1 = self.apply("q1")
        a2 = self.apply("q2", actor="t4c", award="aw4",
                        lines=[{"resource_id": "sp1", "window_id": "w1", "urgency": 10}])
        self.complete_item(self.item(a1)["item_id"])
        self.service.record_fulfillment(request_id="f1", actor_id="p1c",
                                        item_id=self.item(a1)["item_id"], quantity=1)
        self.complete_item(self.item(a2)["item_id"], team="t4c")
        receipt = self.service.adjust_capacity(request_id="cap1", actor_id="p1c", window_id="w1",
                                               delta=-1, reason="提供方场地维修")
        self.assertFalse(receipt.replayed)
        displaced = self.view(self.item(a2)["item_id"])
        self.assertEqual("waitlisted", displaced["state"])
        self.assertTrue(displaced["displaced"])
        self.assertEqual(0, displaced["fulfilled_qty"])
        with self.assertRaises(ValidationError):
            self.service.adjust_capacity(request_id="cap2", actor_id="p1c", window_id="w1",
                                         delta=-1, reason="试图回收已履约")
        self.service.adjust_capacity(request_id="cap3", actor_id="p1c", window_id="w1",
                                     delta=1, reason="维修完成")
        restored = self.view(self.item(a2)["item_id"])
        self.assertEqual("confirmed", restored["state"])
        self.assertFalse(restored["displaced"])

    def test_group_cap_blocks_duplicate_occupation_by_related_teams(self):
        self.resource(resource_id="sp1")
        self.window(window_id="w1", resource_id="sp1")
        self.resource(resource_id="sp2", name="展厅B")
        self.window(window_id="w2", resource_id="sp2", capacity=2)
        self.service.relate_teams(request_id="rel1", actor_id="admin", team_id="t2", other_team_id="t3")
        a2 = self.apply("q2", actor="t2c", award="aw2")
        a3 = self.apply("q3", actor="t3c", award="aw3",
                        lines=[{"resource_id": "sp2", "window_id": "w2", "urgency": 50}])
        self.assertEqual("reserved", self.item(a2)["state"])
        self.assertEqual("waitlisted", self.item(a3)["state"])
        self.assertEqual("group_cap_pending", self.item(a3)["reasons"][0]["code"])
        a4 = self.apply("q4", actor="t4c", award="aw4",
                        lines=[{"resource_id": "sp2", "window_id": "w2", "urgency": 50}])
        self.assertEqual("reserved", self.item(a4)["state"])
        self.service.abandon(request_id="ab2", actor_id="t2c", item_id=self.item(a2)["item_id"])
        self.assertEqual("reserved", self.view(self.item(a3)["item_id"])["state"])

    def test_control_group_identity_is_stable(self):
        self.service.relate_teams(request_id="rel1", actor_id="admin", team_id="t1", other_team_id="t2")
        self.service.relate_teams(request_id="rel2", actor_id="admin", team_id="t2", other_team_id="t3")
        self.assertEqual("t1", self.service.get_team("t3").control_group)

    def test_partial_hold_continues_to_count_toward_group_cap(self):
        self.service.create_policy(request_id="pol-cap", actor_id="admin", rules={
            "group_caps": {"space": 2}})
        self.resource(resource_id="sp1", materials=["license"],
                      milestones=[{"code": "m1", "due_days": 30}])
        self.window(window_id="w1", resource_id="sp1", capacity=5)
        self.resource(resource_id="sp2", name="展厅B")
        self.window(window_id="w2", resource_id="sp2", capacity=5)
        a = self.apply("q1", lines=[{"resource_id": "sp1", "window_id": "w1",
                                     "quantity": 2, "urgency": 50}])
        item_id = self.item(a)["item_id"]
        self.complete_item(item_id)
        self.service.record_fulfillment(request_id="f1", actor_id="p1c", item_id=item_id, quantity=1)
        self.service.record_milestone(actor_id="op1", item_id=item_id, code="m1", passed=False)
        # 已履约 1 单位：同组空间类目仍占 1，只能再申请 1
        a2 = self.apply("q2", lines=[{"resource_id": "sp2", "window_id": "w2",
                                      "quantity": 2, "urgency": 50}])
        self.assertEqual("waitlisted", self.item(a2)["state"])
        self.assertEqual("group_cap_pending", self.item(a2)["reasons"][0]["code"])

    def test_mutex_resources_cannot_coexist_within_group(self):
        self.resource(resource_id="sp1", mutex_group="venue")
        self.window(window_id="w1", resource_id="sp1")
        self.resource(resource_id="ex1", category="exhibition", name="展陈位", mutex_group="venue")
        self.window(window_id="w2", resource_id="ex1")
        a = self.apply("q1", lines=[
            {"resource_id": "sp1", "window_id": "w1", "urgency": 50},
            {"resource_id": "ex1", "window_id": "w2", "urgency": 50}])
        self.assertEqual("reserved", a["items"][0]["state"])
        self.assertEqual("waitlisted", a["items"][1]["state"])
        self.assertEqual("mutex_pending", a["items"][1]["reasons"][0]["code"])
        self.service.abandon(request_id="ab1", actor_id="t1c", item_id=a["items"][0]["item_id"])
        self.assertEqual("reserved", self.view(a["items"][1]["item_id"])["state"])

    def test_prerequisite_resource_must_be_held_before_grant(self):
        self.resource(resource_id="sp1")
        self.window(window_id="w1", resource_id="sp1")
        self.resource(resource_id="ch1", provider="p2", category="channel", name="上架渠道",
                      requires=["sp1"], materials=["contract"], parties=["team", "provider"])
        self.window(window_id="w3", resource_id="ch1")
        a = self.apply("q1", lines=[{"resource_id": "ch1", "window_id": "w3", "urgency": 50}])
        self.assertEqual("waitlisted", self.item(a)["state"])
        self.assertEqual("prerequisite_pending", self.item(a)["reasons"][0]["code"])
        space = self.apply("q2", lines=[{"resource_id": "sp1", "window_id": "w1", "urgency": 50}])
        self.service.sweep_due()
        self.assertEqual("reserved", self.view(self.item(a)["item_id"])["state"])
        # 后置材料与双方确认齐备，但前置仅为预留时不能转正式占用
        channel_item = self.item(a)["item_id"]
        self.service.supply_material(actor_id="t1c", item_id=channel_item, code="contract", content={"v": 1})
        self.service.confirm_party(actor_id="t1c", item_id=channel_item, party="team")
        self.service.confirm_party(actor_id="p2c", item_id=channel_item, party="provider")
        self.assertEqual("reserved", self.view(channel_item)["state"])
        # 前置完成确认后，后置在下一轮推进中转正式占用
        self.complete_item(self.item(space)["item_id"])
        self.service.sweep_due()
        self.assertEqual("confirmed", self.view(channel_item)["state"])

    def test_override_requires_distinct_approver_and_records_fairness(self):
        self.resource(materials=["license"])
        self.window()
        a1 = self.apply("q1")
        a2 = self.apply("q2", actor="t2c", award="aw2")
        a3 = self.apply("q3", actor="t3c", award="aw3",
                        lines=[{"resource_id": "sp1", "window_id": "w1", "urgency": 99}])
        target = self.item(a3)["item_id"]
        proposal = self.service.propose_override(actor_id="op1", item_id=target, reason="重点帮扶")
        with self.assertRaises(PermissionDenied):
            self.service.approve_override(actor_id="op1", proposal_id=proposal["proposal_id"])
        view = self.service.approve_override(actor_id="admin", proposal_id=proposal["proposal_id"])
        self.assertEqual("approved_pending_capacity", view["status"])
        self.assertEqual(1, view["fairness"]["jumped_count"])
        self.assertEqual([self.item(a2)["item_id"]], view["fairness"]["jumped_item_ids"])
        self.assertGreater(view["fairness"]["score_gap"], 0)
        self.assertEqual("v1", view["fairness"]["policy_version"])
        self.service.abandon(request_id="ab1", actor_id="t1c", item_id=self.item(a1)["item_id"])
        self.assertEqual("reserved", self.view(target)["state"])
        self.assertEqual("waitlisted", self.view(self.item(a2)["item_id"])["state"])

    def test_override_cannot_bypass_physical_mutex(self):
        self.resource(resource_id="sp1", mutex_group="venue")
        self.window(window_id="w1", resource_id="sp1")
        self.resource(resource_id="ex1", category="exhibition", name="展陈位", mutex_group="venue")
        self.window(window_id="w2", resource_id="ex1")
        a = self.apply("q1", lines=[
            {"resource_id": "sp1", "window_id": "w1", "urgency": 50},
            {"resource_id": "ex1", "window_id": "w2", "urgency": 50}])
        proposal = self.service.propose_override(actor_id="op1",
                                                 item_id=a["items"][1]["item_id"], reason="强插")
        with self.assertRaises(ConflictError):
            self.service.approve_override(actor_id="admin", proposal_id=proposal["proposal_id"])

    def test_policy_versions_do_not_rewrite_past_decisions(self):
        self.resource(materials=["license"])
        self.window()
        a = self.apply("q2", actor="t2c", award="aw2",
                       lines=[{"resource_id": "sp1", "window_id": "w1", "urgency": 90}])
        item_id = self.item(a)["item_id"]
        before = self.view(item_id)
        self.service.create_policy(request_id="pol2", actor_id="admin", rules={
            "weights": {"award_level": 20, "maturity": 70, "urgency": 10}})
        after = self.view(item_id)
        self.assertEqual("v1", after["policy_version"])
        self.assertEqual(before["score"], after["score"])
        simulation = self.service.simulate_policy(actor_id="au1", item_id=item_id,
                                                  policy_version="v2")
        self.assertNotEqual(simulation["actual"]["score"], simulation["simulated"]["score"])
        self.assertEqual(after["score"], simulation["actual"]["score"])
        self.assertIn("note", simulation)

    def test_provider_sees_only_own_delivery_list(self):
        self.resource(resource_id="sp1", materials=["license"], fulfillment_due_days=10)
        self.window(window_id="w1", resource_id="sp1")
        self.resource(resource_id="ch1", provider="p2", category="channel", name="渠道")
        self.window(window_id="w3", resource_id="ch1")
        a_space = self.apply("q1")
        self.complete_item(self.item(a_space)["item_id"])
        self.apply("q4", actor="t4c", award="aw4",
                   lines=[{"resource_id": "ch1", "window_id": "w3", "urgency": 10}])
        mine = self.service.provider_deliveries(actor_id="p1c")
        self.assertEqual({"sp1"}, {i["resource_id"] for i in mine["items"]})
        self.assertIsNotNone(mine["items"][0]["deliver_by"])
        admin_view = self.service.provider_deliveries(actor_id="admin")
        self.assertEqual({"sp1", "ch1"}, {i["resource_id"] for i in admin_view["items"]})

    def test_application_replays_without_duplicate_effects(self):
        self.resource()
        self.window()
        first = self.apply("q1")
        replay = self.apply("q1")
        self.assertEqual(first["application_id"], replay["application_id"])
        count = self.service.database.connection.execute(
            "SELECT COUNT(*) AS c FROM alloc_items").fetchone()["c"]
        self.assertEqual(1, count)

    def test_window_closed_application_is_rejected(self):
        self.resource()
        self.window(starts="2026-09-01T00:00:00Z", ends="2026-10-01T00:00:00Z")
        a = self.apply("q1")
        self.assertEqual("rejected", self.item(a)["state"])
        self.assertEqual("window_closed", self.item(a)["reasons"][0]["code"])

    def test_waitlist_reasons_are_machine_readable(self):
        self.resource(materials=["license"])
        self.window()
        self.apply("q1")
        a2 = self.apply("q2", actor="t2c", award="aw2")
        reason = self.item(a2)["reasons"][0]
        json.dumps(reason, ensure_ascii=False)
        self.assertEqual({"code", "at", "detail"}, set(reason))
        self.assertEqual(1, reason["detail"]["capacity"])

    def test_auditor_cannot_submit_application(self):
        self.resource()
        self.window()
        with self.assertRaises(PermissionDenied):
            self.apply("q9", actor="au1", award="aw1")


if __name__ == "__main__":
    unittest.main()
