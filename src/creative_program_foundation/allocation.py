"""获奖服务资源分配平台。

在基础服务的权限、幂等、SQLite 事务与哈希审计边界上，实现空间、展陈、融资
对接、宣传、渠道上架五类获奖服务的统一分配。

关键规则：

* 组合申请先形成限时预留，材料齐备并完成多方确认后才转为正式占用；
* 放弃、逾期、里程碑未达标、履约期届满、提供方缩减容量都会释放未兑现份额，
  并按冻结的优先级顺序推进候补；
* 已经兑现的份额永久计入容量，不得回收；
* 互斥资源、同一实际控制团队的重复占位、服务之间的先后依赖在决策与候补
  推进时都会重新校验；
* 人工特批必须由两名不同的操作者分别提出和批准，并记录对候补队列的量化
  公平性影响；
* 评分权重、预留期限等政策以版本形式保存，每条申请行冻结申请时的政策版本，
  后续政策切换不会改写既往决策，审计人员可以横向比较各版本结果；
* 到期与候补推进是“已持久化状态 + 当前时间”的确定性纯函数，不依赖后台
  定时器，服务重启后重复推进得到相同结果。

获奖等级 award_level：1 三等奖 / 2 二等奖 / 3 一等奖 / 4 特等奖，数值越大越好。
成熟度 maturity_level：1 概念 / 2 原型 / 3 试产 / 4 小规模销售 / 5 已具备商品化
条件，数值越大越好。紧急度 urgency：0 至 3，由运营人员登记。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .storage import Database


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SERVICE_TYPES = {
    "space": "空间",
    "exhibition": "展陈",
    "financing": "融资对接",
    "publicity": "宣传",
    "channel": "渠道上架",
}

# 申请行生命周期
LINE_WAITLISTED = "waitlisted"
LINE_RESERVED = "reserved"
LINE_OCCUPIED = "occupied"
LINE_FULFILLED = "fulfilled"
LINE_REJECTED = "rejected"
LINE_EXPIRED = "expired"
LINE_ABANDONED = "abandoned"
LINE_STAGE_FAILED = "stage_failed"
LINE_CAPACITY_REDUCED = "capacity_reduced"

# 仍在占容量但尚未终结的状态
ACTIVE_STATUSES = (LINE_RESERVED, LINE_OCCUPIED)

# 拒绝/阻塞原因代码
R_AWARD = "award_level_insufficient"
R_MATURITY = "maturity_insufficient"
R_WINDOW_NOT_OPEN = "window_not_open"
R_WINDOW_CLOSED = "window_closed"
R_MUTEX = "mutex_conflict"
R_DUPLICATE = "duplicate_holding"
R_DEPENDENCY = "dependency_unmet"
R_DEPENDS_ON_WAITLIST = "depends_on_waitlisted_line"
R_CAPACITY = "capacity_unavailable"
R_RESERVATION_EXPIRED = "reservation_expired"
R_STAGE_FAILED = "milestone_failed"
R_DEADLINE = "fulfillment_deadline_passed"
R_CAPACITY_REDUCED = "capacity_reduced_by_provider"
R_MANUAL_OVERRIDE = "manual_override"
R_PROMOTED = "promoted_from_waitlist"
R_MISSING_MATERIAL = "required_material_missing"
R_MISSING_CONFIRMATION = "required_confirmation_missing"

ALLOCATION_ROLES = frozenset({"admin", "operator", "reviewer", "auditor"})

DEFAULT_POLICY_VERSION = "v1"
DEFAULT_POLICY = {
    "label": "初版分配政策",
    "weights": {"award": 10.0, "maturity": 6.0, "urgency": 4.0},
    "reservation_ttl_hours": 72,
    "required_confirmations": 2,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS al_teams (
    team_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    controller_group_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS al_providers (
    provider_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS al_projects (
    project_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES al_teams(team_id),
    title TEXT NOT NULL,
    award_level INTEGER NOT NULL CHECK(award_level BETWEEN 1 AND 4),
    maturity_level INTEGER NOT NULL CHECK(maturity_level BETWEEN 1 AND 5),
    urgency INTEGER NOT NULL DEFAULT 0 CHECK(urgency BETWEEN 0 AND 3),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS al_contacts (
    actor_id TEXT PRIMARY KEY,
    party TEXT NOT NULL CHECK(party IN ('team','provider')),
    party_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS al_policies (
    policy_version TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    weights_json TEXT NOT NULL,
    reservation_ttl_hours INTEGER NOT NULL CHECK(reservation_ttl_hours > 0),
    required_confirmations INTEGER NOT NULL CHECK(required_confirmations BETWEEN 1 AND 3),
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    active_from TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS al_resources (
    resource_id TEXT PRIMARY KEY,
    service_type TEXT NOT NULL,
    provider_id TEXT NOT NULL REFERENCES al_providers(provider_id),
    title TEXT NOT NULL,
    min_award_level INTEGER NOT NULL,
    min_maturity INTEGER NOT NULL,
    mutex_group TEXT,
    requires_json TEXT NOT NULL DEFAULT '[]',
    required_materials_json TEXT NOT NULL DEFAULT '[]',
    fulfillment_deadline_days INTEGER NOT NULL CHECK(fulfillment_deadline_days > 0),
    commitment TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS al_windows (
    window_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL REFERENCES al_resources(resource_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity >= 0),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS al_window_capacity_changes (
    change_id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL,
    old_capacity INTEGER NOT NULL,
    new_capacity INTEGER NOT NULL,
    requested_capacity INTEGER NOT NULL,
    effective_at TEXT NOT NULL,
    actor_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS al_milestones (
    milestone_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL REFERENCES al_resources(resource_id),
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    due_days INTEGER NOT NULL CHECK(due_days > 0),
    required_qty_ratio REAL NOT NULL DEFAULT 0,
    UNIQUE(resource_id, seq)
);
CREATE TABLE IF NOT EXISTS al_applications (
    application_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES al_projects(project_id),
    team_id TEXT NOT NULL,
    controller_group_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS al_lines (
    line_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES al_applications(application_id),
    seq INTEGER NOT NULL,
    project_id TEXT NOT NULL,
    controller_group_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    window_id TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    status TEXT NOT NULL,
    priority_score REAL NOT NULL,
    waitlist_seq INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    fulfillment_deadline_at TEXT,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    required_materials_json TEXT NOT NULL DEFAULT '[]',
    policy_version TEXT NOT NULL,
    override_id TEXT,
    fulfilled_qty INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(application_id, seq)
);
CREATE TABLE IF NOT EXISTS al_materials (
    material_id TEXT PRIMARY KEY,
    line_id TEXT NOT NULL REFERENCES al_lines(line_id),
    kind TEXT NOT NULL,
    document_ref TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(line_id, kind)
);
CREATE TABLE IF NOT EXISTS al_confirmations (
    line_id TEXT NOT NULL REFERENCES al_lines(line_id),
    party TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(line_id, party)
);
CREATE TABLE IF NOT EXISTS al_line_milestones (
    line_id TEXT NOT NULL REFERENCES al_lines(line_id),
    milestone_id TEXT NOT NULL REFERENCES al_milestones(milestone_id),
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','met','failed')),
    met_at TEXT,
    PRIMARY KEY(line_id, milestone_id)
);
CREATE TABLE IF NOT EXISTS al_deliveries (
    delivery_id TEXT PRIMARY KEY,
    line_id TEXT NOT NULL REFERENCES al_lines(line_id),
    qty INTEGER NOT NULL CHECK(qty > 0),
    cumulative_after INTEGER NOT NULL,
    milestone_seq INTEGER,
    note TEXT NOT NULL DEFAULT '',
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS al_overrides (
    override_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    line_id TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    approved_by TEXT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('proposed','approved','rejected')),
    impact_json TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
"""


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------

class AllocationService:
    """获奖服务资源分配的统一决策与履约服务。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        database.connection.executescript(SCHEMA)
        self._ensure_default_policy()

    # -- 基础工具 -----------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _add_hours(self, value: str, hours: float) -> str:
        return (self._parse(value) + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")

    def _add_days(self, value: str, days: int) -> str:
        return (self._parse(value) + timedelta(days=days)).isoformat().replace("+00:00", "Z")

    def _audit(self, conn, *, actor_id: str, action: str, resource_id: str,
               detail: dict[str, Any], occurred_at: str | None = None) -> None:
        append_event(conn, actor_id=actor_id, action=action, resource_type="allocation",
                     resource_id=resource_id, detail=detail, occurred_at=occurred_at or self._now())

    def _actor(self, conn, actor_id: str):
        row = conn.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    @staticmethod
    def _require_role(actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _contact(self, conn, actor_id: str):
        row = conn.execute("SELECT * FROM al_contacts WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise PermissionDenied("该操作者不是团队或资源方联系人")
        return row

    def _idem(self, conn, *, request_id: str, action: str, payload: dict[str, Any],
              create: Callable[[], tuple[str, str, dict[str, Any]]]):
        payload_hash = digest(payload)
        row = conn.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True}
        resource_type, resource_id, response = create()
        conn.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False}

    def _ensure_default_policy(self) -> None:
        with self.database.transaction(immediate=True) as conn:
            if conn.execute("SELECT 1 FROM al_policies").fetchone() is None:
                now = self._now()
                conn.execute(
                    "INSERT INTO al_policies(policy_version,label,weights_json,"
                    "reservation_ttl_hours,required_confirmations,active,created_at,active_from) "
                    "VALUES(?,?,?,?,?,1,?,?)",
                    (DEFAULT_POLICY_VERSION, DEFAULT_POLICY["label"],
                     canonical_json(DEFAULT_POLICY["weights"]),
                     DEFAULT_POLICY["reservation_ttl_hours"],
                     DEFAULT_POLICY["required_confirmations"], now, "2026-01-01T00:00:00Z"),
                )

    def _active_policy(self, conn) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM al_policies WHERE active=1 ORDER BY active_from DESC LIMIT 1").fetchone()
        if row is None:
            raise ValidationError("尚无生效政策")
        return {"version": row["policy_version"], "label": row["label"],
                "weights": json.loads(row["weights_json"]),
                "ttl_hours": row["reservation_ttl_hours"],
                "required_confirmations": row["required_confirmations"]}

    # -- 登记类接口 ---------------------------------------------------------

    def register_team(self, *, request_id: str, actor_id: str, team_id: str, name: str,
                      controller_group_id: str | None = None) -> dict[str, Any]:
        payload = {"team_id": team_id, "name": name, "controller_group_id": controller_group_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator")
            controller_group_id = controller_group_id or team_id

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO al_teams(team_id,name,controller_group_id,created_at) VALUES(?,?,?,?)",
                        (team_id, name, controller_group_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("团队编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="team.registered", resource_id=team_id,
                            detail={"name": name, "controller_group_id": controller_group_id})
                return "team", team_id, {"team_id": team_id}

            return self._idem(conn, request_id=request_id, action="allocation.register_team",
                              payload=payload, create=create)

    def register_provider(self, *, request_id: str, actor_id: str, provider_id: str,
                          name: str) -> dict[str, Any]:
        payload = {"provider_id": provider_id, "name": name}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO al_providers(provider_id,name,created_at) VALUES(?,?,?)",
                        (provider_id, name, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("资源方编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="provider.registered",
                            resource_id=provider_id, detail={"name": name})
                return "provider", provider_id, {"provider_id": provider_id}

            return self._idem(conn, request_id=request_id, action="allocation.register_provider",
                              payload=payload, create=create)

    def register_project(self, *, request_id: str, actor_id: str, project_id: str,
                         team_id: str, title: str, award_level: int, maturity_level: int,
                         urgency: int = 0) -> dict[str, Any]:
        payload = {"project_id": project_id, "team_id": team_id, "title": title,
                   "award_level": award_level, "maturity_level": maturity_level, "urgency": urgency}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator")
            if conn.execute("SELECT 1 FROM al_teams WHERE team_id=?", (team_id,)).fetchone() is None:
                raise NotFoundError("团队不存在")
            if not 1 <= int(award_level) <= 4:
                raise ValidationError("award_level 取值为 1 至 4")
            if not 1 <= int(maturity_level) <= 5:
                raise ValidationError("maturity_level 取值为 1 至 5")
            if not 0 <= int(urgency) <= 3:
                raise ValidationError("urgency 取值为 0 至 3")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO al_projects(project_id,team_id,title,award_level,"
                        "maturity_level,urgency,created_at) VALUES(?,?,?,?,?,?,?)",
                        (project_id, team_id, title, award_level, maturity_level, urgency, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("项目编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="project.registered", resource_id=project_id,
                            detail={"team_id": team_id, "award_level": award_level,
                                    "maturity_level": maturity_level, "urgency": urgency})
                return "project", project_id, {"project_id": project_id}

            return self._idem(conn, request_id=request_id, action="allocation.register_project",
                              payload=payload, create=create)

    def register_contact(self, *, request_id: str, actor_id: str, contact_actor_id: str,
                         party: str, party_ref: str) -> dict[str, Any]:
        """登记团队或资源方的确认联系人（对应一个既有操作者账号）。"""

        payload = {"contact_actor_id": contact_actor_id, "party": party, "party_ref": party_ref}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin")
            self._actor(conn, contact_actor_id)
            if party == "team":
                if conn.execute("SELECT 1 FROM al_teams WHERE team_id=?", (party_ref,)).fetchone() is None:
                    raise NotFoundError("团队不存在")
            elif party == "provider":
                if conn.execute("SELECT 1 FROM al_providers WHERE provider_id=?", (party_ref,)).fetchone() is None:
                    raise NotFoundError("资源方不存在")
            else:
                raise ValidationError("party 只能是 team 或 provider")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO al_contacts(actor_id,party,party_ref,created_at) VALUES(?,?,?,?)",
                        (contact_actor_id, party, party_ref, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("联系人已经登记") from exc
                self._audit(conn, actor_id=actor_id, action="contact.registered",
                            resource_id=contact_actor_id, detail={"party": party, "party_ref": party_ref})
                return "contact", contact_actor_id, {"actor_id": contact_actor_id}

            return self._idem(conn, request_id=request_id, action="allocation.register_contact",
                              payload=payload, create=create)

    def register_resource(self, *, request_id: str, actor_id: str, resource_id: str,
                          service_type: str, provider_id: str, title: str,
                          min_award_level: int, min_maturity: int,
                          fulfillment_deadline_days: int, commitment: str,
                          mutex_group: str | None = None, requires: list[str] | None = None,
                          required_materials: list[str] | None = None) -> dict[str, Any]:
        payload = {"resource_id": resource_id, "service_type": service_type,
                   "provider_id": provider_id, "title": title,
                   "min_award_level": min_award_level, "min_maturity": min_maturity,
                   "mutex_group": mutex_group, "requires": requires or [],
                   "required_materials": required_materials or [],
                   "fulfillment_deadline_days": fulfillment_deadline_days, "commitment": commitment}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator")
            if service_type not in SERVICE_TYPES:
                raise ValidationError("service_type 不在允许范围内")
            if conn.execute("SELECT 1 FROM al_providers WHERE provider_id=?", (provider_id,)).fetchone() is None:
                raise NotFoundError("资源方不存在")
            requires = requires or []
            for required in requires:
                if conn.execute("SELECT 1 FROM al_resources WHERE resource_id=?", (required,)).fetchone() is None:
                    raise NotFoundError(f"前置服务 {required} 不存在")
            if resource_id in requires:
                raise ValidationError("服务不能依赖自身")
            policy = self._active_policy(conn)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO al_resources(resource_id,service_type,provider_id,title,"
                        "min_award_level,min_maturity,mutex_group,requires_json,"
                        "required_materials_json,fulfillment_deadline_days,commitment,"
                        "policy_version,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
                        (resource_id, service_type, provider_id, title, min_award_level,
                         min_maturity, mutex_group, canonical_json(requires),
                         canonical_json(required_materials or []), fulfillment_deadline_days,
                         commitment, policy["version"], self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("资源编号已经存在或依赖无效") from exc
                self._audit(conn, actor_id=actor_id, action="resource.registered", resource_id=resource_id,
                            detail={"service_type": service_type, "provider_id": provider_id,
                                    "min_award_level": min_award_level, "min_maturity": min_maturity,
                                    "mutex_group": mutex_group, "requires": requires,
                                    "fulfillment_deadline_days": fulfillment_deadline_days,
                                    "commitment": commitment, "policy_version": policy["version"]})
                return "resource", resource_id, {"resource_id": resource_id}

            return self._idem(conn, request_id=request_id, action="allocation.register_resource",
                              payload=payload, create=create)

    def add_window(self, *, request_id: str, actor_id: str, window_id: str, resource_id: str,
                   starts_at: str, ends_at: str, capacity: int) -> dict[str, Any]:
        payload = {"window_id": window_id, "resource_id": resource_id,
                   "starts_at": starts_at, "ends_at": ends_at, "capacity": capacity}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator")
            if conn.execute("SELECT 1 FROM al_resources WHERE resource_id=?", (resource_id,)).fetchone() is None:
                raise NotFoundError("资源不存在")
            if self._parse(starts_at) >= self._parse(ends_at):
                raise ValidationError("时段开始时间必须早于结束时间")
            if int(capacity) < 0:
                raise ValidationError("容量不能为负")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO al_windows(window_id,resource_id,starts_at,ends_at,capacity,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (window_id, resource_id, starts_at, ends_at, capacity, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("时段编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="window.added", resource_id=window_id,
                            detail={"resource_id": resource_id, "starts_at": starts_at,
                                    "ends_at": ends_at, "capacity": capacity})
                return "window", window_id, {"window_id": window_id}

            return self._idem(conn, request_id=request_id, action="allocation.add_window",
                              payload=payload, create=create)

    def add_milestone(self, *, request_id: str, actor_id: str, resource_id: str, seq: int,
                      name: str, due_days: int, required_qty_ratio: float = 0.0) -> dict[str, Any]:
        payload = {"resource_id": resource_id, "seq": seq, "name": name,
                   "due_days": due_days, "required_qty_ratio": required_qty_ratio}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator")
            if conn.execute("SELECT 1 FROM al_resources WHERE resource_id=?", (resource_id,)).fetchone() is None:
                raise NotFoundError("资源不存在")
            if int(due_days) <= 0:
                raise ValidationError("due_days 必须为正整数")
            if not 0.0 <= float(required_qty_ratio) <= 1.0:
                raise ValidationError("required_qty_ratio 取值为 0 至 1")
            milestone_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO al_milestones(milestone_id,resource_id,seq,name,due_days,"
                        "required_qty_ratio) VALUES(?,?,?,?,?,?)",
                        (milestone_id, resource_id, seq, name, due_days, required_qty_ratio),
                    )
                except Exception as exc:
                    raise ConflictError("同一资源的里程碑序号必须唯一") from exc
                self._audit(conn, actor_id=actor_id, action="milestone.defined", resource_id=milestone_id,
                            detail={"resource_id": resource_id, "seq": seq, "due_days": due_days})
                return "milestone", milestone_id, {"milestone_id": milestone_id}

            return self._idem(conn, request_id=request_id, action="allocation.add_milestone",
                              payload=payload, create=create)

    def create_policy(self, *, request_id: str, actor_id: str, policy_version: str, label: str,
                      weights: dict[str, float], reservation_ttl_hours: int,
                      required_confirmations: int = 2, activate: bool = True) -> dict[str, Any]:
        """登记政策版本；activate=True 时原子地停用旧版本，既往决策不受影响。"""

        payload = {"policy_version": policy_version, "label": label, "weights": weights,
                   "reservation_ttl_hours": reservation_ttl_hours,
                   "required_confirmations": required_confirmations, "activate": activate}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin")
            for key in ("award", "maturity", "urgency"):
                if not isinstance(weights.get(key), (int, float)) or weights[key] < 0:
                    raise ValidationError(f"weights.{key} 必须是非负数字")
            if int(reservation_ttl_hours) <= 0:
                raise ValidationError("reservation_ttl_hours 必须为正整数")
            if not 1 <= int(required_confirmations) <= 3:
                raise ValidationError("required_confirmations 取值为 1 至 3")
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                if conn.execute("SELECT 1 FROM al_policies WHERE policy_version=?",
                                (policy_version,)).fetchone():
                    raise ConflictError("政策版本已经存在")
                if activate:
                    conn.execute("UPDATE al_policies SET active=0")
                conn.execute(
                    "INSERT INTO al_policies(policy_version,label,weights_json,"
                    "reservation_ttl_hours,required_confirmations,active,created_at,active_from) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (policy_version, label, canonical_json(weights), reservation_ttl_hours,
                     required_confirmations, 1 if activate else 0, now, now),
                )
                self._audit(conn, actor_id=actor_id, action="policy.created",
                            resource_id=policy_version,
                            detail={"weights": weights, "reservation_ttl_hours": reservation_ttl_hours,
                                    "required_confirmations": required_confirmations,
                                    "activate": activate})
                return "policy", policy_version, {"policy_version": policy_version, "active": bool(activate)}

            return self._idem(conn, request_id=request_id, action="allocation.create_policy",
                              payload=payload, create=create)

    # -- 容量与占用查询 -----------------------------------------------------

    @staticmethod
    def _committed_qty(conn, window_id: str) -> int:
        """窗口已被吃下的容量。

        预留/在占/已兑现按全额计；释放类状态只保留已兑现份额——兑现过的
        服务不得回收。
        """

        row = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN status IN ('reserved','occupied','fulfilled') "
            "THEN quantity ELSE fulfilled_qty END),0) AS qty FROM al_lines WHERE window_id=?",
            (window_id,),
        ).fetchone()
        return int(row["qty"])

    def _group_blocker(self, conn, group_id: str, resource_id: str,
                       exclude_line_id: str | None):
        """返回同实际控制团队的互斥/重复占位阻碍，没有则 None。"""

        resource = conn.execute("SELECT * FROM al_resources WHERE resource_id=?",
                                (resource_id,)).fetchone()
        rows = conn.execute(
            "SELECT l.line_id, l.resource_id, l.status FROM al_lines l "
            "JOIN al_applications a ON a.application_id=l.application_id "
            "WHERE a.controller_group_id=? AND l.status IN ('reserved','occupied')",
            (group_id,),
        ).fetchall()
        for row in rows:
            if row["line_id"] == exclude_line_id:
                continue
            if row["resource_id"] == resource_id:
                return f"{R_DUPLICATE}:{resource_id}"
            other = conn.execute("SELECT * FROM al_resources WHERE resource_id=?",
                                 (row["resource_id"])).fetchone()
            if resource["mutex_group"] and other["mutex_group"] and \
                    resource["mutex_group"] == other["mutex_group"]:
                return f"{R_MUTEX}:{row['resource_id']}"
        return None

    # -- 提交组合申请 -------------------------------------------------------

    @staticmethod
    def _score(project, weights: dict[str, float]) -> float:
        return (weights["award"] * project["award_level"]
                + weights["maturity"] * project["maturity_level"]
                + weights["urgency"] * project["urgency"])

    @staticmethod
    def _order_lines(requested: list[dict[str, Any]], resources: dict[str, dict[str, Any]]
                     ) -> list[int]:
        """按服务依赖对组合内申请行做拓扑排序，返回请求下标顺序。"""

        requested_ids = {item["resource_id"] for item in requested}
        indegree = [0] * len(requested)
        dependents: dict[int, list[int]] = {i: [] for i in range(len(requested))}
        index_by_resource = {item["resource_id"]: i for i, item in enumerate(requested)}
        for i, item in enumerate(requested):
            for required in resources[item["resource_id"]]["requires"]:
                if required in requested_ids:
                    j = index_by_resource[required]
                    indegree[i] += 1
                    dependents[j].append(i)
        ordered: list[int] = []
        frontier = sorted(i for i, degree in enumerate(indegree) if degree == 0)
        while frontier:
            index = frontier.pop(0)
            ordered.append(index)
            for dependent in dependents[index]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    frontier.append(dependent)
            frontier.sort()
        if len(ordered) != len(requested):
            raise ValidationError("组合内服务依赖存在循环")
        return ordered

    def submit_application(self, *, request_id: str, actor_id: str, application_id: str,
                           project_id: str, lines: list[dict[str, Any]]) -> dict[str, Any]:
        """提交组合申请，同步形成预留、候补或拒绝决策。"""

        normalized = [{"resource_id": str(item["resource_id"]),
                       "window_id": str(item["window_id"]),
                       "quantity": int(item.get("quantity", 1))} for item in lines]
        payload = {"application_id": application_id, "project_id": project_id, "lines": normalized}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator", "reviewer")
            project = conn.execute("SELECT * FROM al_projects WHERE project_id=?",
                                   (project_id,)).fetchone()
            if project is None:
                raise NotFoundError("项目不存在")
            team = conn.execute("SELECT * FROM al_teams WHERE team_id=?",
                                (project["team_id"],)).fetchone()
            if not normalized:
                raise ValidationError("至少申请一项服务")
            if len({item["resource_id"] for item in normalized}) != len(normalized):
                raise ValidationError("同一组合内不能重复申请同一资源")
            resources: dict[str, dict[str, Any]] = {}
            windows: dict[str, dict[str, Any]] = {}
            for item in normalized:
                if item["quantity"] <= 0:
                    raise ValidationError("申请数量必须为正整数")
                resource = conn.execute("SELECT * FROM al_resources WHERE resource_id=?",
                                        (item["resource_id"],)).fetchone()
                if resource is None:
                    raise NotFoundError(f"资源 {item['resource_id']} 不存在")
                if not resource["active"]:
                    raise ValidationError(f"资源 {item['resource_id']} 已停止受理")
                window = conn.execute("SELECT * FROM al_windows WHERE window_id=?",
                                      (item["window_id"],)).fetchone()
                if window is None or window["resource_id"] != item["resource_id"]:
                    raise NotFoundError(f"时段 {item['window_id']} 不属于该资源")
                resources[item["resource_id"]] = {
                    "requires": json.loads(resource["requires_json"]),
                    "materials": json.loads(resource["required_materials_json"]),
                    "row": resource,
                }
                windows[item["window_id"]] = window
            policy = self._active_policy(conn)
            now = self._now()
            # 先推进既有到期与候补，保证决策基于“此刻”的确定状态
            self._advance_locked(conn, now)

            def create() -> tuple[str, str, dict[str, Any]]:
                if conn.execute("SELECT 1 FROM al_applications WHERE application_id=?",
                                (application_id,)).fetchone():
                    raise ConflictError("申请编号已经存在")
                conn.execute(
                    "INSERT INTO al_applications(application_id,project_id,team_id,"
                    "controller_group_id,policy_version,created_at) VALUES(?,?,?,?,?,?)",
                    (application_id, project_id, project["team_id"], team["controller_group_id"],
                     policy["version"], now),
                )
                score = self._score(project, policy["weights"])
                # 本组合内已经拿到预留的资源，可满足组内先后依赖
                satisfied_this_portfolio: set[str] = set()
                line_ids: list[str] = []
                decision_summary: list[dict[str, Any]] = []
                for order, index in enumerate(self._order_lines(normalized, resources)):
                    item = normalized[index]
                    resource = resources[item["resource_id"]]["row"]
                    window = windows[item["window_id"]]
                    required_materials = resources[item["resource_id"]]["materials"]
                    line_id = uuid.uuid4().hex
                    line_ids.append(line_id)
                    reasons: list[str] = []

                    if project["award_level"] < resource["min_award_level"]:
                        reasons.append(R_AWARD)
                    if project["maturity_level"] < resource["min_maturity"]:
                        reasons.append(R_MATURITY)
                    if now < window["starts_at"]:
                        reasons.append(R_WINDOW_NOT_OPEN)
                    if now > window["ends_at"]:
                        reasons.append(R_WINDOW_CLOSED)
                    blocker = self._group_blocker(
                        conn, team["controller_group_id"], item["resource_id"], None)
                    if blocker:
                        reasons.append(blocker)
                    missing_dependency = False
                    for required in resources[item["resource_id"]]["requires"]:
                        held = self._project_holds(conn, project_id, required, line_id)
                        if required in satisfied_this_portfolio or held:
                            continue
                        missing_dependency = True
                        if self._portfolio_line_waitlisted(conn, application_id, required):
                            reasons.append(R_DEPENDS_ON_WAITLIST + f":{required}")
                        else:
                            reasons.append(R_DEPENDENCY + f":{required}")
                    hard_blocked = any(
                        not reason.startswith(R_DEPENDENCY)
                        and not reason.startswith(R_DEPENDS_ON_WAITLIST)
                        for reason in reasons)
                    status = LINE_REJECTED if hard_blocked else None
                    expires_at = None

                    if not hard_blocked:
                        if missing_dependency:
                            # 前置服务尚在候补或缺失：本行保留候补位，前置兑现后自动可推进
                            status = LINE_WAITLISTED
                        else:
                            committed = self._committed_qty(conn, item["window_id"])
                            if committed + item["quantity"] <= window["capacity"]:
                                status = LINE_RESERVED
                                expires_at = self._add_hours(now, policy["ttl_hours"])
                                satisfied_this_portfolio.add(item["resource_id"])
                            else:
                                status = LINE_WAITLISTED
                                reasons.append(R_CAPACITY)
                    conn.execute(
                        "INSERT INTO al_lines(line_id,application_id,seq,project_id,"
                        "controller_group_id,resource_id,window_id,quantity,status,"
                        "priority_score,expires_at,reasons_json,required_materials_json,"
                        "policy_version,fulfilled_qty,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                        (line_id, application_id, index, project_id,
                         team["controller_group_id"], item["resource_id"], item["window_id"],
                         item["quantity"], status, score, expires_at,
                         canonical_json(reasons), canonical_json(required_materials),
                         policy["version"], now, now),
                    )
                    if status == LINE_WAITLISTED:
                        seq_row = conn.execute(
                            "SELECT COALESCE(MAX(waitlist_seq),0)+1 AS next FROM al_lines "
                            "WHERE window_id=?", (item["window_id"],)).fetchone()
                        conn.execute("UPDATE al_lines SET waitlist_seq=? WHERE line_id=?",
                                     (seq_row["next"], line_id))
                    decision_summary.append({"seq": index, "status": status, "reasons": reasons})
                self._audit(conn, actor_id=actor_id, action="application.submitted",
                            resource_id=application_id,
                            detail={"project_id": project_id, "policy_version": policy["version"],
                                    "lines": decision_summary})
                return "application", application_id, {"application_id": application_id}

            receipt = self._idem(conn, request_id=request_id,
                                 action="allocation.submit_application",
                                 payload=payload, create=create)
            if receipt["replayed"]:
                receipt["application"] = self._application_view(conn, receipt["resource_id"])
            else:
                receipt["application"] = self._application_view(conn, application_id)
            return receipt

    @staticmethod
    def _project_holds(conn, project_id: str, resource_id: str, exclude_line_id: str | None) -> bool:
        """项目是否已就某资源形成预留/在占/兑现。"""

        sql = ("SELECT 1 FROM al_lines WHERE project_id=? AND resource_id=? "
               "AND status IN ('reserved','occupied','fulfilled')")
        parameters: list[Any] = [project_id, resource_id]
        if exclude_line_id:
            sql += " AND line_id<>?"
            parameters.append(exclude_line_id)
        return conn.execute(sql, parameters).fetchone() is not None

    @staticmethod
    def _portfolio_line_waitlisted(conn, application_id: str, resource_id: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM al_lines WHERE application_id=? AND resource_id=? AND status='waitlisted'",
            (application_id, resource_id),
        ).fetchone() is not None

    # -- 材料、确认与转正 ---------------------------------------------------

    def _load_line(self, conn, application_id: str, line_id: str):
        row = conn.execute("SELECT * FROM al_lines WHERE line_id=? AND application_id=?",
                           (line_id, application_id)).fetchone()
        if row is None:
            raise NotFoundError("申请行不存在")
        return row

    def add_material(self, *, actor_id: str, application_id: str, line_id: str,
                     kind: str, document_ref: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._advance_locked(conn, self._now())
            line = self._load_line(conn, application_id, line_id)
            if line["status"] not in (LINE_RESERVED, LINE_WAITLISTED):
                raise ValidationError("当前状态不能补充材料")
            required = json.loads(line["required_materials_json"])
            if kind not in required:
                raise ValidationError("该材料不在资源要求清单内")
            application = conn.execute(
                "SELECT * FROM al_applications WHERE application_id=?", (application_id,)).fetchone()
            if actor["role"] not in ("admin", "operator"):
                contact = self._contact(conn, actor_id)
                if contact["party"] != "team" or contact["party_ref"] != application["team_id"]:
                    raise PermissionDenied("只有团队联系人可以补充材料")
            material_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO al_materials(material_id,line_id,kind,document_ref,actor_id,created_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(line_id,kind) DO UPDATE SET "
                "document_ref=excluded.document_ref, actor_id=excluded.actor_id, created_at=excluded.created_at",
                (material_id, line_id, kind, document_ref, actor_id, self._now()),
            )
            conn.execute("UPDATE al_lines SET updated_at=? WHERE line_id=?", (self._now(), line_id))
            self._audit(conn, actor_id=actor_id, action="material.added", resource_id=line_id,
                        detail={"kind": kind, "document_ref": document_ref})
            return {"line_id": line_id, "kind": kind, "document_ref": document_ref}

    def confirm_line(self, *, actor_id: str, application_id: str, line_id: str) -> dict[str, Any]:
        """团队或资源方联系人对申请行做出本方确认（多方确认）。"""

        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._advance_locked(conn, self._now())
            line = self._load_line(conn, application_id, line_id)
            if line["status"] != LINE_RESERVED:
                raise ValidationError("只有预留中的申请行需要确认")
            application = conn.execute(
                "SELECT * FROM al_applications WHERE application_id=?", (application_id,)).fetchone()
            resource = conn.execute("SELECT * FROM al_resources WHERE resource_id=?",
                                    (line["resource_id"],)).fetchone()
            contact = self._contact(conn, actor_id)
            if contact["party"] == "team":
                if contact["party_ref"] != application["team_id"]:
                    raise PermissionDenied("不能确认其他团队的申请")
                party = "applicant"
            elif contact["party"] == "provider":
                if contact["party_ref"] != resource["provider_id"]:
                    raise PermissionDenied("不能确认其他资源方的服务")
                party = "provider"
            else:
                raise PermissionDenied("联系人角色无效")
            conn.execute(
                "INSERT INTO al_confirmations(line_id,party,actor_id,created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(line_id,party) DO UPDATE SET actor_id=excluded.actor_id,created_at=excluded.created_at",
                (line_id, party, actor_id, self._now()),
            )
            conn.execute("UPDATE al_lines SET updated_at=? WHERE line_id=?", (self._now(), line_id))
            self._audit(conn, actor_id=actor_id, action="line.confirmed", resource_id=line_id,
                        detail={"party": party})
            return {"line_id": line_id, "party": party}

    def conversion_readiness(self, conn, line) -> list[str]:
        """返回尚未满足的转正条件代码，空列表表示可以转正。"""

        policy = conn.execute("SELECT * FROM al_policies WHERE policy_version=?",
                              (line["policy_version"],)).fetchone()
        missing: list[str] = []
        for kind in json.loads(line["required_materials_json"]):
            if conn.execute("SELECT 1 FROM al_materials WHERE line_id=? AND kind=?",
                            (line["line_id"], kind)).fetchone() is None:
                missing.append(f"{R_MISSING_MATERIAL}:{kind}")
        parties = {row["party"] for row in conn.execute(
            "SELECT party FROM al_confirmations WHERE line_id=?", (line["line_id"],))}
        required = policy["required_confirmations"]
        if len(parties) < required or "applicant" not in parties or \
                (required >= 2 and "provider" not in parties):
            missing.append(R_MISSING_CONFIRMATION)
        return missing

    def convert_line(self, *, actor_id: str, application_id: str, line_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._advance_locked(conn, self._now())
            line = self._load_line(conn, application_id, line_id)
            if line["status"] != LINE_RESERVED:
                raise ValidationError("只有预留中的申请行可以转正")
            missing = self.conversion_readiness(conn, line)
            if missing:
                raise ConflictError("转正条件尚未满足: " + ",".join(missing))
            # 转正瞬间再次校验互斥与重复占位
            blocker = self._group_blocker(
                conn, line["controller_group_id"], line["resource_id"], line["line_id"])
            if blocker:
                raise ConflictError("占用冲突: " + blocker)
            now = self._now()
            resource = conn.execute("SELECT * FROM al_resources WHERE resource_id=?",
                                    (line["resource_id"],)).fetchone()
            deadline = self._add_days(now, resource["fulfillment_deadline_days"])
            conn.execute(
                "UPDATE al_lines SET status='occupied',expires_at=NULL,"
                "fulfillment_deadline_at=?,updated_at=? WHERE line_id=?",
                (deadline, now, line_id),
            )
            for milestone in conn.execute(
                    "SELECT * FROM al_milestones WHERE resource_id=? ORDER BY seq",
                    (line["resource_id"],)):
                conn.execute(
                    "INSERT INTO al_line_milestones(line_id,milestone_id,seq,name,due_at,status) "
                    "VALUES(?,?,?,?,?,'pending')",
                    (line_id, milestone["milestone_id"], milestone["seq"],
                     milestone["name"], self._add_days(now, milestone["due_days"])),
                )
            self._audit(conn, actor_id=actor_id, action="line.occupied", resource_id=line_id,
                        detail={"resource_id": line["resource_id"], "window_id": line["window_id"],
                                "fulfillment_deadline_at": deadline})
            # 转正可能满足其他窗口中同项目后置服务的先后依赖，在“此刻”收敛候补
            seeds = [row["window_id"] for row in conn.execute(
                "SELECT DISTINCT l2.window_id FROM al_lines l2 "
                "JOIN al_resources r2 ON r2.resource_id=l2.resource_id "
                "WHERE l2.project_id=? AND l2.status='waitlisted' AND EXISTS ("
                "SELECT 1 FROM json_each(r2.requires_json) WHERE value=?)",
                (line["project_id"], line["resource_id"])).fetchall()]
            promoted = self._settle_from_release(conn, now, seeds) if seeds else []
            return {"line_id": line_id, "status": LINE_OCCUPIED,
                    "fulfillment_deadline_at": deadline, "promoted": promoted}

    # -- 放弃、履约、里程碑 -------------------------------------------------

    def abandon_line(self, *, actor_id: str, application_id: str, line_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            now = self._now()
            self._advance_locked(conn, now)
            line = self._load_line(conn, application_id, line_id)
            if line["status"] not in (LINE_RESERVED, LINE_OCCUPIED, LINE_WAITLISTED):
                raise ValidationError("当前状态不能放弃")
            application = conn.execute(
                "SELECT * FROM al_applications WHERE application_id=?", (application_id,)).fetchone()
            if actor["role"] not in ("admin", "operator"):
                contact = self._contact(conn, actor_id)
                if contact["party"] != "team" or contact["party_ref"] != application["team_id"]:
                    raise PermissionDenied("只有团队联系人可以放弃申请")
            released_qty = line["quantity"] - line["fulfilled_qty"]
            was_waitlisted = line["status"] == LINE_WAITLISTED
            conn.execute(
                "UPDATE al_lines SET status='abandoned',expires_at=NULL,updated_at=? WHERE line_id=?",
                (now, line_id),
            )
            self._audit(conn, actor_id=actor_id, action="line.abandoned", resource_id=line_id,
                        detail={"released_qty": released_qty,
                                "kept_fulfilled_qty": line["fulfilled_qty"]})
            promoted = []
            if released_qty > 0 and not was_waitlisted:
                promoted = self._settle_from_release(conn, now, [line["window_id"]])
            return {"line_id": line_id, "status": LINE_ABANDONED, "released_qty": released_qty,
                    "promoted": promoted}

    def record_delivery(self, *, actor_id: str, application_id: str, line_id: str,
                        qty: int, milestone_seq: int | None = None, note: str = "") -> dict[str, Any]:
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            now = self._now()
            self._advance_locked(conn, now)
            line = self._load_line(conn, application_id, line_id)
            if line["status"] != LINE_OCCUPIED:
                raise ValidationError("只有正式占用中的申请行可以登记交付")
            resource = conn.execute("SELECT * FROM al_resources WHERE resource_id=?",
                                    (line["resource_id"],)).fetchone()
            if actor["role"] != "admin":
                contact = self._contact(conn, actor_id)
                if contact["party"] != "provider" or contact["party_ref"] != resource["provider_id"]:
                    raise PermissionDenied("只有对应资源方可以登记交付")
            qty = int(qty)
            if qty <= 0:
                raise ValidationError("交付数量必须为正整数")
            cumulative = line["fulfilled_qty"] + qty
            if cumulative > line["quantity"]:
                raise ConflictError("累计交付不能超过占用数量")
            delivery_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO al_deliveries(delivery_id,line_id,qty,cumulative_after,"
                "milestone_seq,note,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (delivery_id, line_id, qty, cumulative, milestone_seq, note, actor_id, now),
            )
            status = LINE_FULFILLED if cumulative == line["quantity"] else LINE_OCCUPIED
            conn.execute(
                "UPDATE al_lines SET fulfilled_qty=?,status=?,updated_at=? WHERE line_id=?",
                (cumulative, status, now, line_id),
            )
            if milestone_seq is not None:
                milestone = conn.execute(
                    "SELECT * FROM al_line_milestones WHERE line_id=? AND seq=?",
                    (line_id, milestone_seq)).fetchone()
                if milestone is None:
                    raise NotFoundError("里程碑不存在")
                conn.execute(
                    "UPDATE al_line_milestones SET status='met',met_at=? WHERE line_id=? AND seq=?",
                    (now, line_id, milestone_seq))
            self._audit(conn, actor_id=actor_id, action="delivery.recorded", resource_id=line_id,
                        detail={"qty": qty, "cumulative_after": cumulative, "status": status,
                                "milestone_seq": milestone_seq})
            return {"delivery_id": delivery_id, "line_id": line_id,
                    "cumulative_after": cumulative, "status": status}

    def mark_milestone(self, *, actor_id: str, application_id: str, line_id: str,
                       milestone_seq: int, met: bool) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator")
            now = self._now()
            self._advance_locked(conn, now)
            line = self._load_line(conn, application_id, line_id)
            if line["status"] != LINE_OCCUPIED:
                raise ValidationError("只有正式占用中的申请行可以更新里程碑")
            milestone = conn.execute(
                "SELECT * FROM al_line_milestones WHERE line_id=? AND seq=?",
                (line_id, milestone_seq)).fetchone()
            if milestone is None:
                raise NotFoundError("里程碑不存在")
            if met:
                conn.execute(
                    "UPDATE al_line_milestones SET status='met',met_at=? WHERE line_id=? AND seq=?",
                    (now, line_id, milestone_seq))
                self._audit(conn, actor_id=actor_id, action="milestone.met", resource_id=line_id,
                            detail={"seq": milestone_seq})
                return {"line_id": line_id, "seq": milestone_seq, "status": "met"}
            released_qty = line["quantity"] - line["fulfilled_qty"]
            conn.execute(
                "UPDATE al_line_milestones SET status='failed' WHERE line_id=? AND seq=?",
                (line_id, milestone_seq))
            self._fail_line_locked(conn, line, LINE_STAGE_FAILED, R_STAGE_FAILED,
                                   milestone["due_at"] or now, actor_id,
                                   extra={"milestone_seq": milestone_seq, "milestone_name": milestone["name"]})
            promoted = self._settle_from_release(conn, now, [line["window_id"]]) \
                if released_qty > 0 else []
            return {"line_id": line_id, "seq": milestone_seq, "status": "failed",
                    "released_qty": released_qty, "promoted": promoted}

    # -- 提供方缩减容量 -----------------------------------------------------

    def reduce_window_capacity(self, *, request_id: str, actor_id: str, window_id: str,
                               new_capacity: int) -> dict[str, Any]:
        payload = {"window_id": window_id, "new_capacity": new_capacity}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator")
            window = conn.execute("SELECT * FROM al_windows WHERE window_id=?",
                                  (window_id,)).fetchone()
            if window is None:
                raise NotFoundError("时段不存在")
            new_capacity = int(new_capacity)
            if new_capacity < 0:
                raise ValidationError("容量不能为负")
            now = self._now()
            self._advance_locked(conn, now)

            def create() -> tuple[str, str, dict[str, Any]]:
                old_capacity = window["capacity"]
                delivered_total = conn.execute(
                    "SELECT COALESCE(SUM(fulfilled_qty),0) AS qty FROM al_lines WHERE window_id=?",
                    (window_id,)).fetchone()["qty"]
                # 已兑现份额不可回收：容量不得低于累计兑现
                effective_capacity = max(new_capacity, int(delivered_total))
                clamped = effective_capacity != new_capacity
                conn.execute("UPDATE al_windows SET capacity=? WHERE window_id=?",
                             (effective_capacity, window_id))
                change_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO al_window_capacity_changes(change_id,window_id,old_capacity,"
                    "new_capacity,requested_capacity,effective_at,actor_id) VALUES(?,?,?,?,?,?,?)",
                    (change_id, window_id, old_capacity, effective_capacity, new_capacity, now, actor_id),
                )
                revoked: list[dict[str, Any]] = []
                excess = self._committed_qty(conn, window_id) - effective_capacity
                if excess > 0:
                    # 先撤预留（弱于正式占用），同档按优先级由低到高，保持稳定顺序
                    candidates = conn.execute(
                        "SELECT * FROM al_lines WHERE window_id=? AND status IN ('reserved','occupied') "
                        "AND fulfilled_qty<quantity ORDER BY CASE status WHEN 'reserved' THEN 0 ELSE 1 END,"
                        "priority_score ASC, created_at DESC, line_id",
                        (window_id,)).fetchall()
                    for candidate in candidates:
                        if excess <= 0:
                            break
                        removable = candidate["quantity"] - candidate["fulfilled_qty"]
                        excess -= removable
                        reasons = json.loads(candidate["reasons_json"])
                        reasons = [r for r in reasons if not r.startswith(R_CAPACITY_REDUCED)]
                        reasons.append(R_CAPACITY_REDUCED)
                        conn.execute(
                            "UPDATE al_lines SET status='capacity_reduced',expires_at=NULL,"
                            "reasons_json=?,updated_at=? WHERE line_id=?",
                            (canonical_json(reasons), now, candidate["line_id"]))
                        revoked.append({"line_id": candidate["line_id"],
                                        "released_qty": removable,
                                        "kept_fulfilled_qty": candidate["fulfilled_qty"]})
                        self._audit(conn, actor_id=actor_id, action="line.capacity_reduced",
                                    resource_id=candidate["line_id"],
                                    detail={"window_id": window_id, "released_qty": removable})
                promoted = self._settle_from_release(conn, now, [window_id])
                self._audit(conn, actor_id=actor_id, action="window.capacity_reduced",
                            resource_id=window_id,
                            detail={"old_capacity": old_capacity,
                                    "requested_capacity": new_capacity,
                                    "effective_capacity": effective_capacity,
                                    "clamped_to_delivered": clamped,
                                    "revoked": revoked, "promoted": promoted})
                return "capacity_change", change_id, {
                    "change_id": change_id, "window_id": window_id,
                    "old_capacity": old_capacity, "requested_capacity": new_capacity,
                    "effective_capacity": effective_capacity,
                    "clamped_to_delivered": clamped, "revoked": revoked, "promoted": promoted}

            return self._idem(conn, request_id=request_id,
                              action="allocation.reduce_capacity", payload=payload, create=create)

    # -- 人工特批（双人批准 + 量化公平性影响） ------------------------------

    def propose_override(self, *, request_id: str, actor_id: str, application_id: str,
                         line_id: str, reason: str) -> dict[str, Any]:
        payload = {"application_id": application_id, "line_id": line_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator")
            line = self._load_line(conn, application_id, line_id)
            if line["status"] in (LINE_RESERVED, LINE_OCCUPIED, LINE_FULFILLED):
                raise ValidationError("已经获配的申请行不需要特批")

            def create() -> tuple[str, str, dict[str, Any]]:
                override_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO al_overrides(override_id,application_id,line_id,proposed_by,"
                    "reason,status,created_at) VALUES(?,?,?,?,?,'proposed',?)",
                    (override_id, application_id, line_id, actor_id, reason, self._now()),
                )
                self._audit(conn, actor_id=actor_id, action="override.proposed",
                            resource_id=override_id,
                            detail={"application_id": application_id, "line_id": line_id,
                                    "proposed_by": actor_id, "reason": reason})
                return "override", override_id, {"override_id": override_id, "status": "proposed"}

            return self._idem(conn, request_id=request_id,
                              action="allocation.propose_override", payload=payload, create=create)

    def decide_override(self, *, actor_id: str, override_id: str, approve: bool) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_role(actor, "admin", "operator")
            override = conn.execute("SELECT * FROM al_overrides WHERE override_id=?",
                                    (override_id)).fetchone()
            if override is None:
                raise NotFoundError("特批单不存在")
            if override["status"] != "proposed":
                raise ConflictError("特批单已经裁决")
            if actor_id == override["proposed_by"]:
                raise PermissionDenied("人工特批必须由提出人之外的第二名操作者批准")
            now = self._now()
            if not approve:
                conn.execute(
                    "UPDATE al_overrides SET status='rejected',approved_by=NULL,decided_at=? "
                    "WHERE override_id=?", (now, override_id))
                self._audit(conn, actor_id=actor_id, action="override.rejected",
                            resource_id=override_id, detail={"decided_by": actor_id})
                return {"override_id": override_id, "status": "rejected"}

            self._advance_locked(conn, now)
            line = conn.execute("SELECT * FROM al_lines WHERE line_id=?",
                                (override["line_id"],)).fetchone()
            if line["status"] in (LINE_RESERVED, LINE_OCCUPIED, LINE_FULFILLED):
                raise ConflictError("申请行已经获配，特批失效")
            # 结构性公平底线：互斥与重复占位即使特批也不允许
            blocker = self._group_blocker(
                conn, line["controller_group_id"], line["resource_id"], line["line_id"])
            if blocker:
                raise PermissionDenied("互斥或重复占用冲突不能通过特批绕过: " + blocker)

            impact = self._override_impact(conn, line)
            policy = conn.execute("SELECT * FROM al_policies WHERE policy_version=?",
                                  (line["policy_version"],)).fetchone()
            ttl = policy["reservation_ttl_hours"]
            conn.execute(
                "UPDATE al_lines SET status='reserved',expires_at=?,reasons_json=?,"
                "override_id=?,updated_at=? WHERE line_id=?",
                (self._add_hours(now, ttl),
                 canonical_json([R_MANUAL_OVERRIDE]), override_id, now, line["line_id"]))
            conn.execute(
                "UPDATE al_overrides SET status='approved',approved_by=?,impact_json=?,decided_at=? "
                "WHERE override_id=?", (actor_id, canonical_json(impact), now, override_id))
            self._audit(conn, actor_id=actor_id, action="override.approved",
                        resource_id=override_id,
                        detail={"line_id": line["line_id"], "proposed_by": override["proposed_by"],
                                "approved_by": actor_id, "impact": impact})
            return {"override_id": override_id, "status": "approved", "impact": impact}

    @staticmethod
    def _override_impact(conn, line) -> dict[str, Any]:
        """量化特批对候补队列的公平性影响。"""

        window = conn.execute("SELECT * FROM al_windows WHERE window_id=?",
                              (line["window_id"],)).fetchone()
        waitlisted = conn.execute(
            "SELECT l.line_id,l.application_id,l.priority_score,a.controller_group_id "
            "FROM al_lines l JOIN al_applications a ON a.application_id=l.application_id "
            "WHERE l.window_id=? AND l.status='waitlisted' ORDER BY l.priority_score DESC,"
            "l.created_at ASC,l.line_id",
            (line["window_id"],)).fetchall()
        skipped = [dict(row) for row in waitlisted
                   if row["priority_score"] > line["priority_score"]]
        committed = AllocationService._committed_qty(conn, line["window_id"])
        overcommit = max(0, committed + line["quantity"] - window["capacity"])
        score_gap = round(skipped[0]["priority_score"] - line["priority_score"], 6) if skipped else 0.0
        return {
            "window_id": line["window_id"],
            "bypassed_decision_reasons": json.loads(line["reasons_json"]),
            "skipped_waitlist_count": len(skipped),
            "skipped_lines": [{"line_id": row["line_id"], "application_id": row["application_id"],
                               "priority_score": row["priority_score"]} for row in skipped],
            "highest_waitlist_score_gap": score_gap,
            "overcommit_qty": overcommit,
            "disadvantaged_controller_groups": sorted(
                {row["controller_group_id"] for row in skipped}),
            "beneficiary_controller_group": line["controller_group_id"],
        }

    # -- 确定性时间推进：到期、失败、候补级联 -------------------------------

    def run_due_processing(self) -> dict[str, int]:
        """对外暴露的确定性推进入口，重复调用幂等。"""

        with self.database.transaction(immediate=True) as conn:
            return self._advance_locked(conn, self._now())

    def _advance_locked(self, conn, now: str) -> dict[str, int]:
        """把所有到期事件按发生时刻依次处理，并在每个释放点级联推进候补。

        候补预留的到期时间从“释放发生的时刻”起算，而不是从实际执行 sweep
        的时刻起算，因此无论何时触发、是否重启，截至同一 now 的结果一致。
        """

        counts = {"expired": 0, "stage_failed": 0, "deadline_failed": 0,
                  "promoted": 0, "rejected": 0}
        # 每条行最多产生一次预留到期、一次履约期届满，每个行里程碑一次逾期；
        # 候补推进只更新既有行，不会产生新的到期事件源。
        bound = self._event_bound(conn)
        for _ in range(bound):
            event = self._next_due_event(conn, now)
            if event is None:
                break
            kind = event["kind"]
            at = event["at"]
            if kind == "expiry":
                line = event["row"]
                self._expire_locked(conn, line, at)
                counts["expired"] += 1
                promoted = self._settle_from_release(conn, at, [line["window_id"]])
                counts["promoted"] += len(promoted)
            elif kind in ("milestone", "deadline"):
                line = event["row"]
                released = line["quantity"] - line["fulfilled_qty"]
                if kind == "milestone":
                    conn.execute(
                        "UPDATE al_line_milestones SET status='failed' WHERE line_id=? AND seq=?",
                        (line["line_id"], event["seq"]))
                    self._fail_line_locked(conn, line, LINE_STAGE_FAILED, R_STAGE_FAILED, at,
                                           "system", {"milestone_seq": event["seq"]})
                    counts["stage_failed"] += 1
                else:
                    self._fail_line_locked(conn, line, LINE_STAGE_FAILED, R_DEADLINE, at,
                                           "system", {"fulfillment_deadline_at": at})
                    counts["deadline_failed"] += 1
                if released > 0:
                    promoted = self._settle_from_release(conn, at, [line["window_id"]])
                    counts["promoted"] += len(promoted)
        return counts

    @staticmethod
    def _event_bound(conn) -> int:
        rows = conn.execute("SELECT COUNT(*) AS c FROM al_lines").fetchone()["c"]
        milestones = conn.execute(
            "SELECT COUNT(*) AS c FROM al_line_milestones WHERE status='pending'").fetchone()["c"]
        return rows * 2 + milestones + 1

    @staticmethod
    def _next_due_event(conn, now: str):
        """返回时间上最早的到期事件（预留到期 / 里程碑逾期 / 履约期届满）。"""

        candidates = []
        row = conn.execute(
            "SELECT * FROM al_lines WHERE status='reserved' AND expires_at<=? "
            "ORDER BY expires_at,line_id LIMIT 1", (now,)).fetchone()
        if row:
            candidates.append((row["expires_at"], row["line_id"], {"kind": "expiry", "at": row["expires_at"], "row": row}))
        row = conn.execute(
            "SELECT l.*, m.seq AS due_seq, m.due_at AS due_at FROM al_lines l "
            "JOIN al_line_milestones m ON m.line_id=l.line_id "
            "WHERE l.status='occupied' AND m.status='pending' AND m.due_at<=? "
            "ORDER BY m.due_at,l.line_id,m.seq LIMIT 1", (now,)).fetchone()
        if row:
            candidates.append((row["due_at"], row["line_id"],
                               {"kind": "milestone", "at": row["due_at"], "row": row,
                                "seq": row["due_seq"]}))
        row = conn.execute(
            "SELECT * FROM al_lines WHERE status='occupied' AND fulfilled_qty<quantity "
            "AND fulfillment_deadline_at IS NOT NULL AND fulfillment_deadline_at<=? "
            "ORDER BY fulfillment_deadline_at,line_id LIMIT 1", (now,)).fetchone()
        if row:
            candidates.append((row["fulfillment_deadline_at"], row["line_id"],
                               {"kind": "deadline", "at": row["fulfillment_deadline_at"], "row": row}))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][2]

    def _expire_locked(self, conn, line, at: str) -> None:
        reasons = [r for r in json.loads(line["reasons_json"])
                   if r not in (R_CAPACITY,)] + [R_RESERVATION_EXPIRED]
        conn.execute(
            "UPDATE al_lines SET status='expired',expires_at=NULL,reasons_json=?,updated_at=? "
            "WHERE line_id=?", (canonical_json(reasons), at, line["line_id"]))
        self._audit(conn, actor_id="system", action="line.expired", resource_id=line["line_id"],
                    detail={"expired_at": at}, occurred_at=at)

    def _fail_line_locked(self, conn, line, status: str, reason: str, at: str,
                          actor_id: str, extra: dict[str, Any]) -> None:
        reasons = [r for r in json.loads(line["reasons_json"])
                   if not r.startswith("milestone") and r != R_DEADLINE] + [reason]
        conn.execute(
            "UPDATE al_lines SET status=?,expires_at=NULL,reasons_json=?,updated_at=? "
            "WHERE line_id=?", (status, canonical_json(reasons), at, line["line_id"]))
        detail = {"released_qty": line["quantity"] - line["fulfilled_qty"],
                  "kept_fulfilled_qty": line["fulfilled_qty"], **extra}
        self._audit(conn, actor_id=actor_id, action="line.stage_failed",
                    resource_id=line["line_id"], detail=detail, occurred_at=at)

    def _promote_for_window(self, conn, window_id: str, at: str) -> list[dict[str, Any]]:
        """在释放时刻 at 为窗口推进候补。

        顺序按冻结的优先级分数降序、申请时间升序。被互斥/依赖暂时挡住的
        条目跳过但保留候补；遇到第一个“就绪但容量不足”的条目即停止，避免
        后来者插队。
        """

        promoted: list[dict[str, Any]] = []
        window = conn.execute("SELECT * FROM al_windows WHERE window_id=?",
                              (window_id,)).fetchone()
        while True:
            candidates = conn.execute(
                "SELECT * FROM al_lines WHERE window_id=? AND status='waitlisted' "
                "ORDER BY priority_score DESC,created_at ASC,line_id",
                (window_id,)).fetchall()
            action = None
            for candidate in candidates:
                eligible, reasons, permanent = self._promotion_eligibility(
                    conn, candidate, window, at)
                if permanent:
                    conn.execute(
                        "UPDATE al_lines SET status='rejected',reasons_json=?,updated_at=? "
                        "WHERE line_id=?",
                        (canonical_json(reasons), at, candidate["line_id"]))
                    self._audit(conn, actor_id="system", action="line.rejected_after_wait",
                                resource_id=candidate["line_id"],
                                detail={"reasons": reasons}, occurred_at=at)
                    action = "rejected"
                    break
                if not eligible:
                    continue
                committed = self._committed_qty(conn, window_id)
                if committed + candidate["quantity"] > window["capacity"]:
                    action = "capacity_stop"
                    break
                policy = conn.execute("SELECT * FROM al_policies WHERE policy_version=?",
                                      (candidate["policy_version"],)).fetchone()
                expiry = self._add_hours(at, policy["reservation_ttl_hours"])
                conn.execute(
                    "UPDATE al_lines SET status='reserved',expires_at=?,"
                    "reasons_json='[]',updated_at=? WHERE line_id=?",
                    (expiry, at, candidate["line_id"]))
                self._audit(conn, actor_id="system", action="line.promoted",
                            resource_id=candidate["line_id"],
                            detail={"window_id": window_id, "released_at": at,
                                    "expires_at": expiry, "policy_version": candidate["policy_version"]},
                            occurred_at=at)
                promoted.append({"line_id": candidate["line_id"], "expires_at": expiry})
                action = "promoted"
                break
            if action in (None, "capacity_stop"):
                break
        return promoted

    def _settle_from_release(self, conn, anchor_at: str, seed_windows) -> list[dict[str, Any]]:
        """在释放时刻 anchor_at 把候补推进收敛到跨窗口稳态。

        被推进的前置服务可能解除其他窗口中后置服务的依赖阻塞，因此按窗口
        做至多“窗口数”轮迭代；所有新预留的期限都锚定在同一个释放时刻，
        保证结果与触发时机、服务重启无关。
        """

        total: list[dict[str, Any]] = []
        dirty = {window_id for window_id in seed_windows}
        rounds = conn.execute("SELECT COUNT(*) AS c FROM al_windows").fetchone()["c"] + 1
        for _ in range(rounds):
            if not dirty:
                break
            windows = sorted(dirty)
            dirty = set()
            for window_id in windows:
                promoted = self._promote_for_window(conn, window_id, anchor_at)
                if not promoted:
                    continue
                total.extend(promoted)
                promoted_ids = ",".join("?" for _ in promoted)
                rows = conn.execute(
                    "SELECT DISTINCT l2.window_id, l2.line_id, r2.requires_json, lp.resource_id AS pred "
                    "FROM al_lines lp JOIN al_lines l2 ON l2.project_id=lp.project_id "
                    "JOIN al_resources r2 ON r2.resource_id=l2.resource_id "
                    f"WHERE lp.line_id IN ({promoted_ids}) AND l2.status='waitlisted'",
                    [item["line_id"] for item in promoted]).fetchall()
                promoted_resources = {
                    row["resource_id"] for row in conn.execute(
                        f"SELECT resource_id FROM al_lines WHERE line_id IN ({promoted_ids})",
                        [item["line_id"] for item in promoted]).fetchall()
                }
                for row in rows:
                    if promoted_resources & set(json.loads(row["requires_json"])):
                        dirty.add(row["window_id"])
        return total

    def _promotion_eligibility(self, conn, line, window, at: str
                               ) -> tuple[bool, list[str], bool]:
        """返回（是否可推进、原因、是否永久拒绝）。"""

        reasons: list[str] = []
        permanent = False
        if at > window["ends_at"]:
            return False, [R_WINDOW_CLOSED], True
        resource = conn.execute("SELECT * FROM al_resources WHERE resource_id=?",
                                (line["resource_id"])).fetchone()
        if not resource["active"]:
            return False, ["resource_inactive"], True
        project = conn.execute("SELECT * FROM al_projects WHERE project_id=?",
                               (line["project_id"])).fetchone()
        if project["award_level"] < resource["min_award_level"]:
            reasons.append(R_AWARD)
        if project["maturity_level"] < resource["min_maturity"]:
            reasons.append(R_MATURITY)
        blocker = self._group_blocker(
            conn, line["controller_group_id"], line["resource_id"], line["line_id"])
        if blocker:
            reasons.append(blocker)
        for required in json.loads(resource["requires_json"]):
            if not self._project_holds(conn, line["project_id"], required, line["line_id"]):
                if self._portfolio_line_waitlisted(conn, line["application_id"], required):
                    reasons.append(R_DEPENDS_ON_WAITLIST + f":{required}")
                else:
                    reasons.append(R_DEPENDENCY + f":{required}")
        # 资源/门槛类原因视为暂时受阻而非永久拒绝（项目资料可能更新）
        return (not reasons), reasons, permanent

    # -- 查询视图 -----------------------------------------------------------

    def get_application(self, application_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as conn:
            # 读视图前做一次确定性推进，保证调用方看到的是“此刻”的状态
            self._advance_locked(conn, self._now())
            return self._application_view(conn, application_id)

    def _application_view(self, conn, application_id: str) -> dict[str, Any]:
        application = conn.execute("SELECT * FROM al_applications WHERE application_id=?",
                                   (application_id,)).fetchone()
        if application is None:
            raise NotFoundError("申请不存在")
        project = conn.execute("SELECT * FROM al_projects WHERE project_id=?",
                               (application["project_id"],)).fetchone()
        lines_view = []
        for line in conn.execute("SELECT * FROM al_lines WHERE application_id=? ORDER BY seq",
                                 (application_id,)):
            resource = conn.execute("SELECT * FROM al_resources WHERE resource_id=?",
                                    (line["resource_id"],)).fetchone()
            window = conn.execute("SELECT * FROM al_windows WHERE window_id=?",
                                  (line["window_id"],)).fetchone()
            materials = [dict(r) for r in conn.execute(
                "SELECT kind,document_ref,actor_id,created_at FROM al_materials WHERE line_id=? ORDER BY kind",
                (line["line_id"],))]
            confirmations = [dict(r) for r in conn.execute(
                "SELECT party,actor_id,created_at FROM al_confirmations WHERE line_id=? ORDER BY party",
                (line["line_id"],))]
            milestones = [dict(r) for r in conn.execute(
                "SELECT seq,name,due_at,status,met_at FROM al_line_milestones WHERE line_id=? ORDER BY seq",
                (line["line_id"],))]
            deliveries = [dict(r) for r in conn.execute(
                "SELECT delivery_id,qty,cumulative_after,milestone_seq,note,actor_id,created_at "
                "FROM al_deliveries WHERE line_id=? ORDER BY created_at", (line["line_id"],))]
            current_blockers: list[str] = []
            waitlist_position = None
            if line["status"] == LINE_WAITLISTED:
                ahead = conn.execute(
                    "SELECT COUNT(*) AS c FROM al_lines WHERE window_id=? AND status='waitlisted' "
                    "AND (priority_score>? OR (priority_score=? AND (created_at<? OR "
                    "(created_at=? AND line_id<?))))",
                    (line["window_id"], line["priority_score"], line["priority_score"],
                     line["created_at"], line["created_at"], line["line_id"])).fetchone()["c"]
                waitlist_position = ahead + 1
                eligible, blockers, _ = self._promotion_eligibility(
                    conn, line, window, self._now())
                if not eligible:
                    current_blockers = blockers
                elif self._committed_qty(conn, line["window_id"]) + line["quantity"] > window["capacity"]:
                    current_blockers = [R_CAPACITY]
            pending = []
            if line["status"] == LINE_RESERVED:
                pending = self.conversion_readiness(conn, line)
            lines_view.append({
                "line_id": line["line_id"], "seq": line["seq"],
                "resource_id": line["resource_id"],
                "service_type": resource["service_type"],
                "resource_title": resource["title"],
                "window_id": line["window_id"],
                "window_starts_at": window["starts_at"], "window_ends_at": window["ends_at"],
                "quantity": line["quantity"], "status": line["status"],
                "reasons": json.loads(line["reasons_json"]),
                "current_blockers": current_blockers,
                "waitlist_position": waitlist_position,
                "priority_score": line["priority_score"],
                "policy_version": line["policy_version"],
                "expires_at": line["expires_at"],
                "fulfillment_deadline_at": line["fulfillment_deadline_at"],
                "fulfilled_qty": line["fulfilled_qty"],
                "required_materials": json.loads(line["required_materials_json"]),
                "materials": materials, "confirmations": confirmations,
                "milestones": milestones, "deliveries": deliveries,
                "pending_conversion_requirements": pending,
                "override_id": line["override_id"],
            })
        return {"application_id": application_id, "project_id": application["project_id"],
                "team_id": application["team_id"],
                "controller_group_id": application["controller_group_id"],
                "policy_version": application["policy_version"],
                "created_at": application["created_at"],
                "project": {"project_id": project["project_id"], "title": project["title"],
                            "award_level": project["award_level"],
                            "maturity_level": project["maturity_level"],
                            "urgency": project["urgency"]},
                "lines": lines_view}

    def provider_deliveries(self, provider_id: str) -> dict[str, Any]:
        """资源方视角的交付清单：只有正式占用（含部分/全额兑现）才出现。"""

        with self.database.transaction(immediate=True) as conn:
            if conn.execute("SELECT 1 FROM al_providers WHERE provider_id=?",
                            (provider_id,)).fetchone() is None:
                raise NotFoundError("资源方不存在")
            self._advance_locked(conn, self._now())
            items = []
            rows = conn.execute(
                "SELECT l.*, r.title AS resource_title, r.service_type, r.commitment, "
                "p.title AS project_title, a.team_id FROM al_lines l "
                "JOIN al_resources r ON r.resource_id=l.resource_id "
                "JOIN al_projects p ON p.project_id=l.project_id "
                "JOIN al_applications a ON a.application_id=l.application_id "
                "WHERE r.provider_id=? AND (l.status IN ('occupied','fulfilled') "
                "OR (l.fulfilled_qty>0 AND l.status IN ('stage_failed','capacity_reduced','abandoned'))) "
                "ORDER BY l.fulfillment_deadline_at,l.line_id", (provider_id,)).fetchall()
            for row in rows:
                window = conn.execute("SELECT * FROM al_windows WHERE window_id=?",
                                      (row["window_id"],)).fetchone()
                milestones = [dict(m) for m in conn.execute(
                    "SELECT seq,name,due_at,status,met_at FROM al_line_milestones "
                    "WHERE line_id=? ORDER BY seq", (row["line_id"],))]
                items.append({
                    "line_id": row["line_id"], "application_id": row["application_id"],
                    "resource_id": row["resource_id"], "resource_title": row["resource_title"],
                    "service_type": row["service_type"], "window_id": row["window_id"],
                    "window_starts_at": window["starts_at"], "window_ends_at": window["ends_at"],
                    "commitment": row["commitment"],
                    "project_id": row["project_id"], "project_title": row["project_title"],
                    "team_id": row["team_id"], "status": row["status"],
                    "quantity": row["quantity"], "fulfilled_qty": row["fulfilled_qty"],
                    "open_qty": row["quantity"] - row["fulfilled_qty"],
                    "fulfillment_deadline_at": row["fulfillment_deadline_at"],
                    "policy_version": row["policy_version"], "milestones": milestones,
                })
            return {"provider_id": provider_id, "count": len(items), "items": items}

    def list_policies(self) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM al_policies ORDER BY active_from").fetchall()
        return [{"policy_version": row["policy_version"], "label": row["label"],
                 "weights": json.loads(row["weights_json"]),
                 "reservation_ttl_hours": row["reservation_ttl_hours"],
                 "required_confirmations": row["required_confirmations"],
                 "active": bool(row["active"]), "active_from": row["active_from"]} for row in rows]

    def policy_comparison(self) -> dict[str, Any]:
        """审计视角：按冻结政策版本汇总决策结果与特批的公平性影响。

        只读取已冻结的快照，不重新评分、不回写任何历史行。
        """

        with self.database.transaction(immediate=True) as conn:
            versions = []
            for policy in conn.execute("SELECT * FROM al_policies ORDER BY active_from").fetchall():
                stats: dict[str, int] = {}
                for row in conn.execute(
                        "SELECT status,COUNT(*) AS c FROM al_lines WHERE policy_version=? GROUP BY status",
                        (policy["policy_version"],)):
                    stats[row["status"]] = row["c"]
                override_stats = {"count": 0, "total_skipped_waitlist": 0,
                                  "total_overcommit_qty": 0, "max_score_gap": 0.0,
                                  "disadvantaged_groups": set()}
                for override in conn.execute(
                        "SELECT impact_json FROM al_overrides WHERE status='approved' "
                        "AND line_id IN (SELECT line_id FROM al_lines WHERE policy_version=?)",
                        (policy["policy_version"],)):
                    impact = json.loads(override["impact_json"])
                    override_stats["count"] += 1
                    override_stats["total_skipped_waitlist"] += impact["skipped_waitlist_count"]
                    override_stats["total_overcommit_qty"] += impact["overcommit_qty"]
                    override_stats["max_score_gap"] = max(
                        override_stats["max_score_gap"], impact["highest_waitlist_score_gap"])
                    override_stats["disadvantaged_groups"].update(
                        impact["disadvantaged_controller_groups"])
                versions.append({
                    "policy_version": policy["policy_version"], "label": policy["label"],
                    "weights": json.loads(policy["weights_json"]),
                    "reservation_ttl_hours": policy["reservation_ttl_hours"],
                    "required_confirmations": policy["required_confirmations"],
                    "active": bool(policy["active"]), "active_from": policy["active_from"],
                    "line_counts": stats,
                    "lines_total": sum(stats.values()),
                    "overrides": {
                        "count": override_stats["count"],
                        "total_skipped_waitlist": override_stats["total_skipped_waitlist"],
                        "total_overcommit_qty": override_stats["total_overcommit_qty"],
                        "max_score_gap": round(override_stats["max_score_gap"], 6),
                        "disadvantaged_controller_groups": sorted(
                            override_stats["disadvantaged_groups"]),
                    },
                })
            return {"versions": versions}
