import unittest

from creative_program_foundation.allocation import AllocationService
from creative_program_foundation.api import route
from creative_program_foundation.clock import ManualClock
from creative_program_foundation.storage import Database
from datetime import datetime, timezone


HEADERS = {
    "admin": {"X-Actor-Id": "admin-001"},
    "operator": {"X-Actor-Id": "op-001"},
    "team1": {"X-Actor-Id": "t1c"},
    "team2": {"X-Actor-Id": "t2c"},
    "provider1": {"X-Actor-Id": "p1c"},
    "auditor": {"X-Actor-Id": "au1"},
}


class AllocationApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database(":memory:")
        self.service = AllocationService(
            self.database, ManualClock(datetime(2026, 10, 3, 8, tzinfo=timezone.utc)))
        self._seed()

    def _call(self, method, path, body=None, actor="admin"):
        return route(self.service, method, path, body or {}, HEADERS.get(actor, {}))

    def _seed(self):
        bootstrap = {"X-Actor-Id": "bootstrap"}
        status, _ = route(self.service, "POST", "/organizations",
                          {"request_id": "org", "organization_id": "o1", "name": "运营方"}, bootstrap)
        assert status == 201
        status, _ = route(self.service, "POST", "/actors",
                          {"request_id": "a1", "new_actor_id": "admin-001",
                           "display_name": "管理员", "role": "admin",
                           "organization_id": "o1"}, bootstrap)
        assert status == 201
        for rid, actor_id, role, name in [
                ("a2", "op-001", "operator", "运营"),
                ("a3", "t1c", "applicant", "甲联系人"), ("a4", "t2c", "applicant", "乙联系人"),
                ("a5", "p1c", "provider", "空间商"), ("a6", "au1", "auditor", "审计")]:
            self._call("POST", "/actors",
                       {"request_id": rid, "new_actor_id": actor_id, "display_name": name,
                        "role": role, "organization_id": "o1"})
        self._call("POST", "/teams", {"request_id": "t1", "team_id": "team1",
                                      "name": "甲队", "contact_actor_id": "t1c"})
        self._call("POST", "/teams", {"request_id": "t2", "team_id": "team2",
                                      "name": "乙队", "contact_actor_id": "t2c"})
        self._call("POST", "/providers", {"request_id": "p1", "provider_id": "prov1",
                                          "name": "空间商", "contact_actor_id": "p1c"})
        self._call("POST", "/awards", {"request_id": "aw1", "award_id": "aw1", "team_id": "team1",
                                       "award_level": "grand", "title": "甲作品", "maturity_score": 90})
        self._call("POST", "/awards", {"request_id": "aw2", "award_id": "aw2", "team_id": "team2",
                                       "award_level": "second", "title": "乙作品", "maturity_score": 60})
        self._call("POST", "/resources",
                   {"request_id": "r1", "resource_id": "sp1", "provider_id": "prov1",
                    "category": "space", "name": "展厅A", "materials": ["license"],
                    "parties": ["team", "provider"]})
        self._call("POST", "/windows",
                   {"request_id": "w1", "window_id": "win1", "resource_id": "sp1",
                    "starts_at": "2026-10-01T00:00:00Z", "ends_at": "2026-11-30T00:00:00Z",
                    "capacity": 1})

    def tearDown(self):
        self.database.close()

    def test_health_reports_audit_on_allocation_service(self):
        status, payload = self._call("GET", "/health")
        self.assertEqual(200, status)
        self.assertTrue(payload["audit_valid"])

    def test_application_lifecycle_over_http(self):
        status, payload = self._call(
            "POST", "/applications",
            {"request_id": "app1", "award_id": "aw1",
             "items": [{"resource_id": "sp1", "window_id": "win1", "urgency": 80}]}, actor="team1")
        self.assertEqual(201, status)
        app1 = payload["application_id"]
        status, payload = self._call(
            "POST", "/applications",
            {"request_id": "app2", "award_id": "aw2",
             "items": [{"resource_id": "sp1", "window_id": "win1", "urgency": 90}]},
            actor="team2")
        self.assertEqual(201, status)
        app2 = payload["application_id"]

        status, payload = self._call("GET", f"/applications/{app1}", actor="team1")
        self.assertEqual(200, status)
        item_id = payload["items"][0]["item_id"]
        self.assertEqual("reserved", payload["items"][0]["state"])

        # 材料与双方确认
        self.assertEqual(200, self._call("POST", f"/items/{item_id}/materials",
                                         {"code": "license", "content": {"doc": 1}},
                                         actor="team1")[0])
        self.assertEqual(200, self._call("POST", f"/items/{item_id}/confirmations",
                                         {"party": "team"}, actor="team1")[0])
        self.assertEqual(200, self._call("POST", f"/items/{item_id}/confirmations",
                                         {"party": "provider"}, actor="provider1")[0])
        status, payload = self._call("GET", f"/applications/{app1}", actor="team1")
        self.assertEqual("confirmed", payload["items"][0]["state"])

        # 乙在候补，原因可机读
        status, payload = self._call("GET", "/waitlist?window_id=win1", actor="operator")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        self.assertEqual("insufficient_capacity", payload["items"][0]["reasons"][0]["code"])

        # 提供方只能看到自己的交付清单
        status, payload = self._call("GET", "/provider-deliveries", actor="provider1")
        self.assertEqual(200, status)
        self.assertEqual({"sp1"}, {i["resource_id"] for i in payload["items"]})
        return app1, app2, item_id

    def test_other_team_cannot_read_application(self):
        status, payload = self._call(
            "POST", "/applications",
            {"request_id": "app1", "award_id": "aw1",
             "items": [{"resource_id": "sp1", "window_id": "win1"}]}, actor="team1")
        app1 = payload["application_id"]
        status, payload = self._call("GET", f"/applications/{app1}", actor="team2")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_auditor_cannot_create_resource(self):
        status, payload = self._call(
            "POST", "/resources",
            {"request_id": "rx", "resource_id": "spx", "provider_id": "prov1",
             "category": "space", "name": "X"}, actor="auditor")
        self.assertEqual(403, status)

    def test_invalid_capacity_returns_400(self):
        status, payload = self._call(
            "POST", "/windows",
            {"request_id": "wbad", "window_id": "winbad", "resource_id": "sp1",
             "starts_at": "2026-12-01T00:00:00Z", "ends_at": "2026-11-01T00:00:00Z",
             "capacity": 1})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_missing_field_is_400(self):
        status, payload = self._call("POST", "/teams", {"request_id": "tx", "team_id": "team9"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_sweep_and_override_routes(self):
        app1, app2, item1 = self.test_application_lifecycle_over_http()
        # 双人特批：乙的候补需要另一人批准
        status, payload = self._call("GET", f"/applications/{app2}", actor="team2")
        item2 = payload["items"][0]["item_id"]
        status, proposal = self._call("POST", "/overrides",
                                      {"item_id": item2, "reason": "重点项目"}, actor="operator")
        self.assertEqual(201, status)
        status, payload = self._call("POST", f"/overrides/{proposal['proposal_id']}/approve",
                                     {}, actor="operator")
        self.assertEqual(403, status)
        status, payload = self._call("POST", f"/overrides/{proposal['proposal_id']}/approve",
                                     {}, actor="admin")
        self.assertEqual(200, status)
        self.assertEqual("approved_pending_capacity", payload["status"])

        # 放弃后特批项越过队列获配
        status, payload = self._call("POST", f"/items/{item1}/abandon",
                                     {"request_id": "ab1"}, actor="team1")
        self.assertEqual(201, status)
        status, payload = self._call("GET", f"/applications/{app2}", actor="team2")
        self.assertEqual("reserved", payload["items"][0]["state"])
        self.assertTrue(any(r["code"] == "override_granted"
                            for r in payload["items"][0]["reasons"]))

    def test_capacity_adjustment_route(self):
        self._call("POST", "/applications",
                   {"request_id": "app1", "award_id": "aw1",
                    "items": [{"resource_id": "sp1", "window_id": "win1"}]}, actor="team1")
        status, payload = self._call(
            "POST", "/windows/win1/capacity",
            {"request_id": "cap1", "delta": 1, "reason": "增加档期"}, actor="provider1")
        self.assertEqual(201, status)
        self.assertEqual(2, payload["capacity"])

    def test_policy_simulation_route(self):
        status, payload = self._call(
            "POST", "/applications",
            {"request_id": "app2", "award_id": "aw2",
             "items": [{"resource_id": "sp1", "window_id": "win1", "urgency": 90}]}, actor="team2")
        app2 = payload["application_id"]
        self._call("POST", "/policies", {"request_id": "pol2", "rules": {
            "weights": {"award_level": 20, "maturity": 70, "urgency": 10}}})
        status, app = self._call("GET", f"/applications/{app2}", actor="admin")
        item_id = app["items"][0]["item_id"]
        status, payload = self._call(
            "GET", f"/items/{item_id}/policy-simulation?policy_version=v2", actor="auditor")
        self.assertEqual(200, status)
        self.assertNotEqual(0, payload["score_delta"])


if __name__ == "__main__":
    unittest.main()
