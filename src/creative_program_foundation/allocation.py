"""获奖服务资源分配：在基础服务边界上实现组合申请、限时预留、稳定候补与公平审计。

核心决策对象：

- 获奖作品（等级、成熟度）归属于团队，团队之间通过关联关系形成“实际控制组”；
- 资源（空间、展陈、融资对接、宣传、渠道上架）由提供方承诺分时段容量，
  可声明互斥组、先后依赖、必备材料与多方确认；
- 组合申请按提交时生效的政策版本打分并快照，容量充足时形成限时预留，
  材料与确认齐备后转为正式占用，否则进入稳定候补或被拒绝；
- 放弃、逾期、里程碑未达标与提供方缩减容量都会在同一事务内释放未履约部分，
  已履约部分永久占用容量，释放出的容量按稳定候补顺序重新分配；
- 人工特批必须双人批准，并量化记录对候补队列公平性的影响。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .audit import append_event, canonical_json
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .service import DomainService
from .storage import Database


# 获奖后可申请的五类服务
CATEGORIES = frozenset({"space", "exhibition", "financing", "promotion", "channel"})
AWARD_LEVELS = ("excellence", "third", "second", "first", "grand")
ACTIVE_STATES = ("reserved", "confirmed")

ALLOCATION_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS alloc_teams (
    team_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    contact_actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alloc_team_edges (
    team_id TEXT NOT NULL,
    other_team_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(team_id, other_team_id)
);
CREATE TABLE IF NOT EXISTS alloc_providers (
    provider_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    contact_actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alloc_awards (
    award_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    award_level TEXT NOT NULL,
    title TEXT NOT NULL,
    maturity_score INTEGER NOT NULL CHECK(maturity_score BETWEEN 0 AND 100),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alloc_resources (
    resource_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    min_level_rank INTEGER NOT NULL DEFAULT 1 CHECK(min_level_rank BETWEEN 1 AND 5),
    min_maturity INTEGER NOT NULL DEFAULT 0 CHECK(min_maturity BETWEEN 0 AND 100),
    mutex_group TEXT,
    requires_json TEXT NOT NULL DEFAULT '[]',
    materials_json TEXT NOT NULL DEFAULT '[]',
    parties_json TEXT NOT NULL DEFAULT '[]',
    milestones_json TEXT NOT NULL DEFAULT '[]',
    fulfillment_due_days INTEGER,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alloc_windows (
    window_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alloc_window_commitments (
    commitment_id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL,
    capacity_delta INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alloc_policies (
    policy_version TEXT PRIMARY KEY,
    rules_json TEXT NOT NULL,
    rules_hash TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alloc_applications (
    application_id TEXT PRIMARY KEY,
    award_id TEXT NOT NULL,
    team_id TEXT NOT NULL,
    control_group TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    policy_version TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alloc_items (
    item_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    window_id TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    urgency INTEGER NOT NULL DEFAULT 0 CHECK(urgency BETWEEN 0 AND 100),
    state TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    score_components_json TEXT NOT NULL DEFAULT '{}',
    reasons_json TEXT NOT NULL DEFAULT '[]',
    facts_json TEXT NOT NULL DEFAULT '{}',
    displaced_flag INTEGER NOT NULL DEFAULT 0 CHECK(displaced_flag IN (0,1)),
    override_flag INTEGER NOT NULL DEFAULT 0 CHECK(override_flag IN (0,1)),
    expires_at TEXT,
    granted_at TEXT,
    confirmed_at TEXT,
    fulfilled_qty INTEGER NOT NULL DEFAULT 0,
    released_qty INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alloc_items_window_state ON alloc_items(window_id, state);
CREATE INDEX IF NOT EXISTS idx_alloc_items_application ON alloc_items(application_id);
CREATE TABLE IF NOT EXISTS alloc_item_materials (
    item_id TEXT NOT NULL,
    code TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    supplied_by TEXT NOT NULL,
    supplied_at TEXT NOT NULL,
    PRIMARY KEY(item_id, code)
);
CREATE TABLE IF NOT EXISTS alloc_item_confirmations (
    item_id TEXT NOT NULL,
    party TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY(item_id, party)
);
CREATE TABLE IF NOT EXISTS alloc_item_milestones (
    item_id TEXT NOT NULL,
    code TEXT NOT NULL,
    due_at TEXT,
    passed INTEGER CHECK(passed IN (0,1)),
    decided_by TEXT,
    decided_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(item_id, code)
);
CREATE TABLE IF NOT EXISTS alloc_fulfillments (
    fulfillment_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    note TEXT NOT NULL DEFAULT '',
    delivered_by TEXT NOT NULL,
    delivered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alloc_overrides (
    proposal_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    proposed_at TEXT NOT NULL,
    approver_id TEXT,
    approved_at TEXT,
    status TEXT NOT NULL,
    fairness_json TEXT NOT NULL DEFAULT '{}'
);
"""


DEFAULT_RULES: dict[str, Any] = {
    "reservation_ttl_minutes": 2880,
    "weights": {"award_level": 50, "maturity": 35, "urgency": 15},
    "level_rank": {"excellence": 1, "third": 2, "second": 3, "first": 4, "grand": 5},
    # 同一实际控制组在每个服务类目上的同时在占容量上限，防止多作品重复占位
    "group_caps": {"space": 1, "exhibition": 1, "financing": 1, "promotion": 2, "channel": 2},
}


@dataclass(frozen=True)
class Team:
    team_id: str
    name: str
    contact_actor_id: str
    control_group: str


@dataclass(frozen=True)
class Award:
    award_id: str
    team_id: str
    award_level: str
    title: str
    maturity_score: int


@dataclass(frozen=True)
class Resource:
    resource_id: str
    provider_id: str
    category: str
    name: str
    min_level_rank: int
    min_maturity: int
    mutex_group: str | None
    requires: tuple[str, ...]
    materials: tuple[str, ...]
    parties: tuple[str, ...]
    milestones: tuple[dict[str, Any], ...]
    fulfillment_due_days: int | None


@dataclass(frozen=True)
class ResourceWindow:
    window_id: str
    resource_id: str
    starts_at: str
    ends_at: str
    capacity: int
    consumed: int


@dataclass(frozen=True)
class OverrideProposal:
    proposal_id: str
    item_id: str
    reason: str
    proposed_by: str
    proposed_at: str
    approver_id: str | None
    approved_at: str | None
    status: str
    fairness: dict[str, Any]


class AllocationService(DomainService):
    """协调获奖服务资源的申请、预留、占用、履约与再分配。"""

    def __init__(self, database: Database, clock=None) -> None:
        super().__init__(database, clock)
        database.connection.executescript(ALLOCATION_SCHEMA)
        self._seed_default_policy()

    # ------------------------------------------------------------------ 基础工具

    def _seed_default_policy(self) -> None:
        row = self.database.connection.execute("SELECT COUNT(*) AS c FROM alloc_policies").fetchone()
        if row["c"]:
            return
        now = self._now()
        self.database.connection.execute(
            "INSERT INTO alloc_policies(policy_version,rules_json,rules_hash,active,created_by,created_at) "
            "VALUES(?,?,?,1,'system',?)",
            ("v1", canonical_json(DEFAULT_RULES), _hash_rules(DEFAULT_RULES), now),
        )
        append_event(self.database.connection, actor_id="system", action="policy.seeded",
                     resource_type="policy", resource_id="v1",
                     detail={"rules_hash": _hash_rules(DEFAULT_RULES)}, occurred_at=now)

    def _ts(self, value: str, field: str) -> str:
        value = self._text(value, field, 40)
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _parse(self, value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _add_minutes(self, value: str, minutes: int) -> str:
        return (self._parse(value) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")

    def _add_days(self, value: str, days: int) -> str:
        return (self._parse(value) + timedelta(days=days)).isoformat().replace("+00:00", "Z")

    def _active_policy(self, connection) -> tuple[str, dict[str, Any]]:
        row = connection.execute(
            "SELECT * FROM alloc_policies WHERE active=1 ORDER BY policy_version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise NotFoundError("当前没有生效的分配政策")
        import json
        return row["policy_version"], json.loads(row["rules_json"])

    def _policy(self, connection, version: str) -> dict[str, Any]:
        import json
        row = connection.execute("SELECT * FROM alloc_policies WHERE policy_version=?", (version,)).fetchone()
        if row is None:
            raise NotFoundError("政策版本不存在")
        return json.loads(row["rules_json"])

    def _score(self, rules: dict[str, Any], *, level_rank: int, maturity: int, urgency: int) -> tuple[float, dict]:
        weights = rules["weights"]
        max_rank = max(rules["level_rank"].values())
        components = {
            "award_level": round(weights["award_level"] * level_rank / max_rank, 3),
            "maturity": round(weights["maturity"] * maturity / 100, 3),
            "urgency": round(weights["urgency"] * urgency / 100, 3),
        }
        return round(sum(components.values()), 3), components

    def _reason(self, code: str, **detail: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {"code": code, "at": self._now()}
        if detail:
            entry["detail"] = detail
        return entry

    def _control_group(self, connection, team_id: str) -> str:
        """对全部关联边做并查集，返回最小成员编号作为稳定控制组标识。"""

        parent: dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            root = x
            while parent[root] != root:
                root = parent[root]
            while parent[x] != root:
                parent[x], x = root, parent[x]
            return root

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                if rb < ra:
                    ra, rb = rb, ra
                parent[rb] = ra

        for row in connection.execute("SELECT team_id, other_team_id FROM alloc_team_edges"):
            union(row["team_id"], row["other_team_id"])
        return find(team_id)

    def _load_resource(self, connection, resource_id: str) -> Resource:
        import json
        row = connection.execute("SELECT * FROM alloc_resources WHERE resource_id=?", (resource_id,)).fetchone()
        if row is None:
            raise NotFoundError("资源不存在")
        return Resource(
            row["resource_id"], row["provider_id"], row["category"], row["name"],
            row["min_level_rank"], row["min_maturity"], row["mutex_group"],
            tuple(json.loads(row["requires_json"])), tuple(json.loads(row["materials_json"])),
            tuple(json.loads(row["parties_json"])), tuple(json.loads(row["milestones_json"])),
            row["fulfillment_due_days"],
        )

    def _load_window(self, connection, window_id: str):
        row = connection.execute("SELECT * FROM alloc_windows WHERE window_id=?", (window_id,)).fetchone()
        if row is None:
            raise NotFoundError("资源时段不存在")
        return row

    def _window_capacity(self, connection, window_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(SUM(capacity_delta),0) AS cap FROM alloc_window_commitments WHERE window_id=?",
            (window_id,),
        ).fetchone()
        return row["cap"]

    def _window_consumed(self, connection, window_id: str) -> int:
        """在占项目占全部未释放数量；被挤回候补的项目仍永久占用已履约部分。"""

        row = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN state IN ('reserved','confirmed','fulfilled') "
            "THEN quantity-released_qty ELSE fulfilled_qty END),0) AS used "
            "FROM alloc_items WHERE window_id=?",
            (window_id,),
        ).fetchone()
        return row["used"]

    def _window_fulfilled(self, connection, window_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(SUM(fulfilled_qty),0) AS done FROM alloc_items WHERE window_id=?",
            (window_id,),
        ).fetchone()
        return row["done"]

    def _group_category_held(self, connection, group: str, category: str) -> int:
        """同组在某类目上的占用；被挤回候补的项目其已履约部分仍占位。"""

        row = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN i.state IN ('reserved','confirmed','fulfilled') "
            "THEN i.quantity-i.released_qty ELSE i.fulfilled_qty END),0) AS held "
            "FROM alloc_items i "
            "JOIN alloc_applications a ON a.application_id=i.application_id "
            "JOIN alloc_resources r ON r.resource_id=i.resource_id "
            "WHERE a.control_group=? AND r.category=?",
            (group, category),
        ).fetchone()
        return row["held"]

    def _mutex_active(self, connection, group: str, mutex_group: str, exclude_item: str | None = None) -> str | None:
        sql = (
            "SELECT i.item_id FROM alloc_items i "
            "JOIN alloc_applications a ON a.application_id=i.application_id "
            "JOIN alloc_resources r ON r.resource_id=i.resource_id "
            "WHERE a.control_group=? AND r.mutex_group=? AND i.state IN ('reserved','confirmed')"
        )
        parameters: list[Any] = [group, mutex_group]
        if exclude_item:
            sql += " AND i.item_id<>?"
            parameters.append(exclude_item)
        sql += " LIMIT 1"
        row = connection.execute(sql, parameters).fetchone()
        return row["item_id"] if row else None

    def _team_resource_state(self, connection, team_id: str, resource_id: str) -> str | None:
        row = connection.execute(
            "SELECT i.state FROM alloc_items i "
            "JOIN alloc_applications a ON a.application_id=i.application_id "
            "WHERE a.team_id=? AND i.resource_id=? AND i.state IN ('reserved','confirmed','fulfilled') "
            "ORDER BY CASE i.state WHEN 'fulfilled' THEN 0 WHEN 'confirmed' THEN 1 ELSE 2 END LIMIT 1",
            (team_id, resource_id),
        ).fetchone()
        return row["state"] if row else None

    def _get_item(self, connection, item_id: str):
        row = connection.execute("SELECT * FROM alloc_items WHERE item_id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("申请明细不存在")
        return row

    def _team_of_item(self, connection, item) -> str:
        row = connection.execute(
            "SELECT team_id FROM alloc_applications WHERE application_id=?",
            (item["application_id"],),
        ).fetchone()
        return row["team_id"]

    def _append_reason(self, connection, item, reason: dict[str, Any]) -> None:
        import json
        reasons = json.loads(item["reasons_json"])
        reasons.append(reason)
        connection.execute("UPDATE alloc_items SET reasons_json=?, updated_at=? WHERE item_id=?",
                           (canonical_json(reasons), self._now(), item["item_id"]))

    # ------------------------------------------------------------------ 登记

    def register_team(self, *, request_id: str, actor_id: str, team_id: str,
                      name: str, contact_actor_id: str):
        payload = {"actor_id": actor_id, "team_id": team_id, "name": name,
                   "contact_actor_id": contact_actor_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            contact = self._actor(connection, contact_actor_id)
            if contact.role not in ("applicant", "admin"):
                raise ValidationError("团队联系人必须是 applicant 角色")
            team_id = self._identifier(team_id, "team_id")
            name = self._text(name, "name")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO alloc_teams(team_id,name,contact_actor_id,created_at) VALUES(?,?,?,?)",
                        (team_id, name, contact_actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("团队编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="team.registered",
                             resource_type="team", resource_id=team_id,
                             detail={"name": name, "contact_actor_id": contact_actor_id},
                             occurred_at=self._now())
                return "team", team_id, {"team_id": team_id}

            return self._idempotent(connection, request_id=request_id, action="register_team",
                                    payload=payload, create=create)

    def relate_teams(self, *, request_id: str, actor_id: str, team_id: str, other_team_id: str):
        """登记团队关联（无向），用于识别同一实际控制组。"""

        payload = {"actor_id": actor_id, "team_id": team_id, "other_team_id": other_team_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            team_id = self._identifier(team_id, "team_id")
            other_team_id = self._identifier(other_team_id, "other_team_id")
            if team_id == other_team_id:
                raise ValidationError("团队不能与自身关联")
            for tid in (team_id, other_team_id):
                if connection.execute("SELECT 1 FROM alloc_teams WHERE team_id=?", (tid,)).fetchone() is None:
                    raise NotFoundError(f"团队 {tid} 不存在")

            def create():
                connection.execute(
                    "INSERT OR IGNORE INTO alloc_team_edges(team_id,other_team_id,created_by,created_at) "
                    "VALUES(?,?,?,?)",
                    (team_id, other_team_id, actor_id, self._now()),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO alloc_team_edges(team_id,other_team_id,created_by,created_at) "
                    "VALUES(?,?,?,?)",
                    (other_team_id, team_id, actor_id, self._now()),
                )
                group = self._control_group(connection, team_id)
                append_event(connection, actor_id=actor_id, action="team.related",
                             resource_type="team_relation", resource_id=f"{team_id}:{other_team_id}",
                             detail={"team_id": team_id, "other_team_id": other_team_id,
                                     "control_group": group}, occurred_at=self._now())
                return "team_relation", f"{team_id}:{other_team_id}", {"control_group": group}

            return self._idempotent(connection, request_id=request_id, action="relate_teams",
                                    payload=payload, create=create)

    def register_provider(self, *, request_id: str, actor_id: str, provider_id: str,
                          name: str, contact_actor_id: str):
        payload = {"actor_id": actor_id, "provider_id": provider_id, "name": name,
                   "contact_actor_id": contact_actor_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            contact = self._actor(connection, contact_actor_id)
            if contact.role not in ("provider", "admin"):
                raise ValidationError("提供方联系人必须是 provider 角色")
            provider_id = self._identifier(provider_id, "provider_id")
            name = self._text(name, "name")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO alloc_providers(provider_id,name,contact_actor_id,created_at) VALUES(?,?,?,?)",
                        (provider_id, name, contact_actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("提供方编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="provider.registered",
                             resource_type="provider", resource_id=provider_id,
                             detail={"name": name, "contact_actor_id": contact_actor_id},
                             occurred_at=self._now())
                return "provider", provider_id, {"provider_id": provider_id}

            return self._idempotent(connection, request_id=request_id, action="register_provider",
                                    payload=payload, create=create)

    def register_award(self, *, request_id: str, actor_id: str, award_id: str, team_id: str,
                       award_level: str, title: str, maturity_score: int):
        payload = {"actor_id": actor_id, "award_id": award_id, "team_id": team_id,
                   "award_level": award_level, "title": title, "maturity_score": maturity_score}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            award_id = self._identifier(award_id, "award_id")
            if award_level not in AWARD_LEVELS:
                raise ValidationError("award_level 不合法")
            title = self._text(title, "title")
            if not isinstance(maturity_score, int) or not 0 <= maturity_score <= 100:
                raise ValidationError("maturity_score 必须是 0 到 100 的整数")
            if connection.execute("SELECT 1 FROM alloc_teams WHERE team_id=?", (team_id,)).fetchone() is None:
                raise NotFoundError("团队不存在")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO alloc_awards(award_id,team_id,award_level,title,maturity_score,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (award_id, team_id, award_level, title, maturity_score, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("获奖作品编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="award.registered",
                             resource_type="award", resource_id=award_id,
                             detail={"team_id": team_id, "award_level": award_level,
                                     "maturity_score": maturity_score}, occurred_at=self._now())
                return "award", award_id, {"award_id": award_id}

            return self._idempotent(connection, request_id=request_id, action="register_award",
                                    payload=payload, create=create)

    def register_resource(self, *, request_id: str, actor_id: str, resource_id: str, provider_id: str,
                          category: str, name: str, min_level: str = "excellence",
                          min_maturity: int = 0, mutex_group: str | None = None,
                          requires: list[str] | None = None, materials: list[str] | None = None,
                          parties: list[str] | None = None, milestones: list[dict] | None = None,
                          fulfillment_due_days: int | None = None):
        requires = requires or []
        materials = materials or []
        parties = parties or ["team", "provider"]
        milestones = milestones or []
        payload = {"actor_id": actor_id, "resource_id": resource_id, "provider_id": provider_id,
                   "category": category, "name": name, "min_level": min_level,
                   "min_maturity": min_maturity, "mutex_group": mutex_group, "requires": requires,
                   "materials": materials, "parties": parties, "milestones": milestones,
                   "fulfillment_due_days": fulfillment_due_days}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            resource_id = self._identifier(resource_id, "resource_id")
            name = self._text(name, "name")
            if category not in CATEGORIES:
                raise ValidationError("category 不合法")
            rules = self._active_policy(connection)[1]
            if min_level not in rules["level_rank"]:
                raise ValidationError("min_level 不在政策等级表中")
            if not isinstance(min_maturity, int) or not 0 <= min_maturity <= 100:
                raise ValidationError("min_maturity 必须是 0 到 100 的整数")
            if connection.execute("SELECT 1 FROM alloc_providers WHERE provider_id=?", (provider_id,)).fetchone() is None:
                raise NotFoundError("提供方不存在")
            if not isinstance(requires, list) or not all(isinstance(x, str) for x in requires):
                raise ValidationError("requires 必须是资源编号列表")
            for required in requires:
                if required == resource_id:
                    raise ValidationError("资源不能依赖自身")
                if connection.execute("SELECT 1 FROM alloc_resources WHERE resource_id=?", (required,)).fetchone() is None:
                    raise NotFoundError(f"依赖资源 {required} 不存在")
            if not isinstance(materials, list) or not all(isinstance(x, str) and x for x in materials):
                raise ValidationError("materials 必须是非空字符串列表")
            allowed_parties = {"team", "provider", "operator"}
            if not isinstance(parties, list) or not parties or not all(p in allowed_parties for p in parties):
                raise ValidationError("parties 只能包含 team/provider/operator 且不能为空")
            if not isinstance(milestones, list):
                raise ValidationError("milestones 必须是列表")
            for milestone in milestones:
                if not isinstance(milestone, dict) or not str(milestone.get("code", "")).strip():
                    raise ValidationError("每个里程碑必须包含 code")
                if not isinstance(milestone.get("due_days", 0), int) or milestone["due_days"] < 0:
                    raise ValidationError("里程碑 due_days 必须是非负整数")
            if fulfillment_due_days is not None and (not isinstance(fulfillment_due_days, int) or fulfillment_due_days < 0):
                raise ValidationError("fulfillment_due_days 必须是非负整数")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO alloc_resources(resource_id,provider_id,category,name,min_level_rank,"
                        "min_maturity,mutex_group,requires_json,materials_json,parties_json,milestones_json,"
                        "fulfillment_due_days,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
                        (resource_id, provider_id, category, name, rules["level_rank"][min_level],
                         min_maturity, mutex_group, canonical_json(requires), canonical_json(materials),
                         canonical_json(parties), canonical_json(milestones), fulfillment_due_days,
                         self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("资源编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="resource.registered",
                             resource_type="resource", resource_id=resource_id,
                             detail={"provider_id": provider_id, "category": category,
                                     "mutex_group": mutex_group, "requires": requires},
                             occurred_at=self._now())
                return "resource", resource_id, {"resource_id": resource_id}

            return self._idempotent(connection, request_id=request_id, action="register_resource",
                                    payload=payload, create=create)

    def register_window(self, *, request_id: str, actor_id: str, window_id: str, resource_id: str,
                        starts_at: str, ends_at: str, capacity: int, note: str = ""):
        payload = {"actor_id": actor_id, "window_id": window_id, "resource_id": resource_id,
                   "starts_at": starts_at, "ends_at": ends_at, "capacity": capacity, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "provider")
            window_id = self._identifier(window_id, "window_id")
            resource_row = connection.execute(
                "SELECT * FROM alloc_resources WHERE resource_id=?", (resource_id,)
            ).fetchone()
            if resource_row is None:
                raise NotFoundError("资源不存在")
            if actor.role == "provider":
                provider = connection.execute(
                    "SELECT * FROM alloc_providers WHERE provider_id=?", (resource_row["provider_id"],)
                ).fetchone()
                if actor.actor_id != provider["contact_actor_id"]:
                    raise PermissionDenied("提供方只能为自己的资源登记时段")
            starts = self._ts(starts_at, "starts_at")
            ends = self._ts(ends_at, "ends_at")
            if not starts < ends:
                raise ValidationError("时段开始必须早于结束")
            if not isinstance(capacity, int) or capacity < 0:
                raise ValidationError("capacity 必须是非负整数")
            note = self._text(note, "note", 500) if note else ""

            def create():
                try:
                    connection.execute(
                        "INSERT INTO alloc_windows(window_id,resource_id,starts_at,ends_at,note,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (window_id, resource_id, starts, ends, note, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("时段编号已经存在") from exc
                commitment_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO alloc_window_commitments(commitment_id,window_id,capacity_delta,reason,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (commitment_id, window_id, capacity, "初始提供方承诺", actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="window.registered",
                             resource_type="window", resource_id=window_id,
                             detail={"resource_id": resource_id, "capacity": capacity,
                                     "starts_at": starts, "ends_at": ends}, occurred_at=self._now())
                return "window", window_id, {"window_id": window_id, "capacity": capacity}

            return self._idempotent(connection, request_id=request_id, action="register_window",
                                    payload=payload, create=create)

    def create_policy(self, *, request_id: str, actor_id: str, rules: dict[str, Any]):
        """发布新政策版本；既往决策保留各自的版本快照，不被改写。"""

        payload = {"actor_id": actor_id, "rules": rules}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            merged = _validate_rules(rules)

            def create():
                row = connection.execute(
                    "SELECT policy_version FROM alloc_policies ORDER BY policy_version DESC LIMIT 1"
                ).fetchone()
                next_number = 1 if row is None else int(row["policy_version"].lstrip("v")) + 1
                version = f"v{next_number}"
                connection.execute("UPDATE alloc_policies SET active=0")
                connection.execute(
                    "INSERT INTO alloc_policies(policy_version,rules_json,rules_hash,active,created_by,created_at) "
                    "VALUES(?,?,?,1,?,?)",
                    (version, canonical_json(merged), _hash_rules(merged), actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="policy.created",
                             resource_type="policy", resource_id=version,
                             detail={"rules_hash": _hash_rules(merged), "rules": merged},
                             occurred_at=self._now())
                return "policy", version, {"policy_version": version, "rules": merged}

            return self._idempotent(connection, request_id=request_id, action="create_policy",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 申请与决策

    def submit_application(self, *, request_id: str, actor_id: str, award_id: str,
                           items: list[dict[str, Any]]):
        """提交组合申请，逐条形成预留、候补或拒绝，全部在一个事务内决策。"""

        payload = {"actor_id": actor_id, "award_id": award_id, "items": items}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            award = connection.execute("SELECT * FROM alloc_awards WHERE award_id=?", (award_id,)).fetchone()
            if award is None:
                raise NotFoundError("获奖作品不存在")
            team = connection.execute("SELECT * FROM alloc_teams WHERE team_id=?", (award["team_id"],)).fetchone()
            if actor.role != "admin" and actor.actor_id != team["contact_actor_id"]:
                raise PermissionDenied("只能由团队联系人提交本团队申请")
            if not isinstance(items, list) or not items:
                raise ValidationError("items 必须是非空列表")
            group = self._control_group(connection, award["team_id"])
            policy_version, rules = self._active_policy(connection)
            now = self._now()

            seen_windows: set[str] = set()
            parsed_lines = []
            for index, line in enumerate(items):
                if not isinstance(line, dict):
                    raise ValidationError(f"第 {index + 1} 条明细必须是对象")
                resource = self._load_resource(connection, line.get("resource_id", ""))
                window = self._load_window(connection, line.get("window_id", ""))
                if window["resource_id"] != resource.resource_id:
                    raise ValidationError(f"时段 {window['window_id']} 不属于资源 {resource.resource_id}")
                if window["window_id"] in seen_windows:
                    raise ValidationError("同一资源时段在一份组合申请中不能重复申请")
                seen_windows.add(window["window_id"])
                quantity = line.get("quantity", 1)
                if not isinstance(quantity, int) or quantity <= 0:
                    raise ValidationError("quantity 必须是正整数")
                urgency = line.get("urgency", 0)
                if not isinstance(urgency, int) or not 0 <= urgency <= 100:
                    raise ValidationError("urgency 必须是 0 到 100 的整数")
                parsed_lines.append((resource, window, quantity, urgency))

            # 组合内部按先后依赖拓扑排序，保证前置先决策
            by_resource = {line[0].resource_id: index for index, line in enumerate(parsed_lines)}
            ordered: list[tuple[Any, Any, int, int]] = []
            placed: set[int] = set()

            def place(index: int) -> None:
                if index in placed:
                    return
                resource = parsed_lines[index][0]
                for prereq in resource.requires:
                    if prereq in by_resource:
                        place(by_resource[prereq])
                placed.add(index)
                ordered.append(parsed_lines[index])

            for index in range(len(parsed_lines)):
                place(index)
            parsed_lines = ordered

            application_id = uuid.uuid4().hex

            def create():
                self._sweep_locked(connection)
                connection.execute(
                    "INSERT INTO alloc_applications(application_id,award_id,team_id,control_group,request_id,"
                    "policy_version,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (application_id, award_id, award["team_id"], group, request_id, policy_version,
                     actor_id, now),
                )
                # 本批次内已预留的类目占用与互斥占用
                batch_category: dict[str, int] = {}
                batch_window: dict[str, int] = {}
                batch_mutex: dict[str, str] = {}
                batch_resource: dict[str, str] = {}
                results = []
                for resource, window, quantity, urgency in parsed_lines:
                    item_id = uuid.uuid4().hex
                    level_rank = rules["level_rank"][award["award_level"]]
                    score, components = self._score(
                        rules, level_rank=level_rank,
                        maturity=award["maturity_score"], urgency=urgency,
                    )
                    facts = {"award_level": award["award_level"], "level_rank": level_rank,
                             "maturity_score": award["maturity_score"], "urgency": urgency,
                             "resource_id": resource.resource_id, "window_id": window["window_id"],
                             "quantity": quantity}
                    reasons: list[dict[str, Any]] = []
                    hard_reject = None
                    if level_rank < resource.min_level_rank:
                        hard_reject = "ineligible_award_level"
                        reasons.append(self._reason(hard_reject, required_rank=resource.min_level_rank,
                                                    actual_rank=level_rank))
                    elif award["maturity_score"] < resource.min_maturity:
                        hard_reject = "ineligible_maturity"
                        reasons.append(self._reason(hard_reject, required=resource.min_maturity,
                                                    actual=award["maturity_score"]))
                    elif now > window["ends_at"]:
                        hard_reject = "window_closed"
                        reasons.append(self._reason(hard_reject, ends_at=window["ends_at"]))

                    state = "rejected" if hard_reject else "waitlisted"
                    if not hard_reject:
                        # 软性条件不满足时进入候补，释放后会按稳定顺序重试
                        cap = rules["group_caps"].get(resource.category)
                        held_category = self._group_category_held(connection, group, resource.category) \
                            + batch_category.get(resource.category, 0)
                        if cap is not None and held_category + quantity > cap:
                            reasons.append(self._reason("group_cap_pending", category=resource.category,
                                                        cap=cap, held=held_category))
                        if resource.mutex_group:
                            active = self._mutex_active(connection, group, resource.mutex_group)
                            active = active or batch_mutex.get(resource.mutex_group)
                            if active:
                                reasons.append(self._reason("mutex_pending",
                                                            mutex_group=resource.mutex_group,
                                                            blocking_item=active))
                        missing_prereq = None
                        for prereq in resource.requires:
                            held_state = self._team_resource_state(connection, award["team_id"], prereq)
                            held_state = held_state or batch_resource.get(prereq)
                            if held_state is None:
                                missing_prereq = prereq
                                reasons.append(self._reason("prerequisite_pending", resource_id=prereq))
                                break
                        capacity = self._window_capacity(connection, window["window_id"])
                        consumed = self._window_consumed(connection, window["window_id"]) \
                            + batch_window.get(window["window_id"], 0)
                        if consumed + quantity > capacity:
                            reasons.append(self._reason("insufficient_capacity", capacity=capacity,
                                                        consumed=consumed, requested=quantity))
                        if not reasons:
                            state = "reserved"
                    connection.execute(
                        "INSERT INTO alloc_items(item_id,application_id,resource_id,window_id,quantity,urgency,"
                        "state,policy_version,score,score_components_json,reasons_json,facts_json,"
                        "expires_at,granted_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (item_id, application_id, resource.resource_id, window["window_id"], quantity, urgency,
                         state, policy_version, score, canonical_json(components), canonical_json(reasons),
                         canonical_json(facts),
                         self._add_minutes(now, rules["reservation_ttl_minutes"]) if state == "reserved" else None,
                         now if state == "reserved" else None, now, now),
                    )
                    action = {"reserved": "item.reserved", "waitlisted": "item.waitlisted",
                              "rejected": "item.rejected"}[state]
                    append_event(connection, actor_id=actor_id, action=action,
                                 resource_type="allocation_item", resource_id=item_id,
                                 detail={"application_id": application_id, "resource_id": resource.resource_id,
                                         "window_id": window["window_id"], "quantity": quantity,
                                         "policy_version": policy_version, "score": score,
                                         "reasons": reasons}, occurred_at=now)
                    if state == "reserved":
                        batch_category[resource.category] = batch_category.get(resource.category, 0) + quantity
                        batch_window[window["window_id"]] = batch_window.get(window["window_id"], 0) + quantity
                        if resource.mutex_group:
                            batch_mutex[resource.mutex_group] = item_id
                        batch_resource[resource.resource_id] = "reserved"
                    results.append({"item_id": item_id, "resource_id": resource.resource_id,
                                    "window_id": window["window_id"], "quantity": quantity,
                                    "state": state, "score": score, "reasons": reasons})
                append_event(connection, actor_id=actor_id, action="application.submitted",
                             resource_type="application", resource_id=application_id,
                             detail={"award_id": award_id, "control_group": group,
                                     "policy_version": policy_version, "item_count": len(results)},
                             occurred_at=now)
                return "application", application_id, {"application_id": application_id,
                                                       "policy_version": policy_version, "items": results}

            return self._idempotent(connection, request_id=request_id, action="submit_application",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 到期与候补推进

    def sweep_due(self, *, actor_id: str = "system") -> dict[str, Any]:
        """处理到期预留并推进候补。幂等，重启后重复执行结果一致。"""

        with self.database.transaction(immediate=True) as connection:
            actor = None
            if actor_id != "system":
                actor = self._actor(connection, actor_id)
                self._require(actor, "admin", "operator")
            return self._sweep_locked(connection, actor_id=actor_id)

    def _sweep_locked(self, connection, *, actor_id: str = "system") -> dict[str, Any]:
        now = self._now()
        expired = []
        for row in connection.execute(
            "SELECT * FROM alloc_items WHERE state='reserved' AND expires_at<=?", (now,)
        ).fetchall():
            self._release_item(connection, row, "reservation_expired", terminal_state="expired",
                               actor_id=actor_id)
            expired.append(row["item_id"])
        closed_rejects = []
        for row in connection.execute(
            "SELECT i.* FROM alloc_items i JOIN alloc_windows w ON w.window_id=i.window_id "
            "WHERE i.state='waitlisted' AND w.ends_at<=?", (now,)
        ).fetchall():
            reasons = _json(row["reasons_json"])
            reasons.append(self._reason("window_closed", at=now))
            connection.execute(
                "UPDATE alloc_items SET state='rejected', reasons_json=?, updated_at=? WHERE item_id=?",
                (canonical_json(reasons), now, row["item_id"]),
            )
            append_event(connection, actor_id=actor_id, action="item.rejected",
                         resource_type="allocation_item", resource_id=row["item_id"],
                         detail={"reason": "window_closed"}, occurred_at=now)
            closed_rejects.append(row["item_id"])
        # 不动点：就绪的预留转正式占用后，可能释放前置阻塞让更多候补获配
        advanced: list[dict[str, Any]] = []
        confirmed: list[str] = []
        while True:
            new_confirmations = self._confirm_ready_reservations(connection, actor_id=actor_id)
            new_advances = self._advance_waitlists(connection, actor_id=actor_id)
            confirmed.extend(new_confirmations)
            advanced.extend(new_advances)
            if not new_confirmations and not new_advances:
                break
        return {"expired": expired, "closed_rejected": closed_rejects,
                "confirmed": confirmed, "advanced": advanced}

    def _confirm_ready_reservations(self, connection, *, actor_id: str) -> list[str]:
        confirmed: list[str] = []
        for row in connection.execute(
                "SELECT * FROM alloc_items WHERE state='reserved' ORDER BY rowid").fetchall():
            resource = self._load_resource(connection, row["resource_id"])
            if self._try_confirm(connection, row, resource, actor_id=actor_id):
                confirmed.append(row["item_id"])
        return confirmed

    def _advance_waitlists(self, connection, *, actor_id: str) -> list[dict[str, Any]]:
        """对每个候补队列按稳定顺序发放新释放的容量。"""

        advanced: list[dict[str, Any]] = []
        windows = connection.execute(
            "SELECT DISTINCT window_id FROM alloc_items WHERE state='waitlisted'"
        ).fetchall()
        for w in windows:
            window_id = w["window_id"]
            window = self._load_window(connection, window_id)
            capacity = self._window_capacity(connection, window_id)
            candidates = connection.execute(
                "SELECT i.* FROM alloc_items i WHERE i.state='waitlisted' AND i.window_id=? "
                "ORDER BY i.override_flag DESC, i.displaced_flag DESC, i.score DESC, "
                "i.created_at, i.item_id",
                (window_id,),
            ).fetchall()
            for candidate in candidates:
                consumed = self._window_consumed(connection, window_id)
                needed = candidate["quantity"] - candidate["fulfilled_qty"] - candidate["released_qty"]
                if consumed + needed > capacity:
                    # 该候选及其后任何候选需要的容量都不少于当前余量，严格截断
                    break
                blocker = self._soft_blocker(connection, candidate, now=self._now(), window=window)
                if blocker == "window_closed":
                    reasons = _json(candidate["reasons_json"])
                    reasons.append(self._reason("window_closed"))
                    connection.execute(
                        "UPDATE alloc_items SET state='rejected', reasons_json=?, updated_at=? WHERE item_id=?",
                        (canonical_json(reasons), self._now(), candidate["item_id"]),
                    )
                    continue
                if blocker:
                    # 队首仍被互斥/依赖/组上限阻塞时，跳过并尝试后续候选，保持稳定顺序
                    continue
                self._grant_from_waitlist(connection, candidate, actor_id=actor_id)
                advanced.append({"item_id": candidate["item_id"], "window_id": window_id})
        return advanced

    def _soft_blocker(self, connection, item, *, now: str, window=None) -> str | None:
        """返回候补项当前仍无法发放的软性原因；None 表示可以发放。"""

        window = window or self._load_window(connection, item["window_id"])
        if now > window["ends_at"]:
            return "window_closed"
        app = connection.execute("SELECT * FROM alloc_applications WHERE application_id=?",
                                 (item["application_id"],)).fetchone()
        resource = self._load_resource(connection, item["resource_id"])
        facts = _json(item["facts_json"])
        rules = self._policy(connection, item["policy_version"])
        cap = rules["group_caps"].get(resource.category)
        if cap is not None:
            held = self._group_category_held(connection, app["control_group"], resource.category)
            if held + item["quantity"] > cap:
                return "group_cap"
        if resource.mutex_group and self._mutex_active(
                connection, app["control_group"], resource.mutex_group, exclude_item=item["item_id"]):
            return "mutex"
        for prereq in resource.requires:
            state = self._team_resource_state(connection, app["team_id"], prereq)
            # 前置仅需已预留即可排队获配；转正式确认时再要求前置已确认
            if state is None:
                return "prerequisite"
        return None

    def _grant_from_waitlist(self, connection, item, *, actor_id: str) -> None:
        now = self._now()
        resource = self._load_resource(connection, item["resource_id"])
        rules = self._policy(connection, item["policy_version"])
        app = connection.execute("SELECT * FROM alloc_applications WHERE application_id=?",
                                 (item["application_id"],)).fetchone()
        prerequisites_confirmed = all(
            self._team_resource_state(connection, app["team_id"], prereq) in ("confirmed", "fulfilled")
            for prereq in resource.requires
        )
        complete = self._requirements_complete(connection, item, resource, check_prerequisite=False) \
            and prerequisites_confirmed
        if complete:
            # 曾被容量缩减挤出、材料、确认与前置服务仍齐备的项目直接恢复正式占用
            state, expires, confirmed_at = "confirmed", None, now
        else:
            state, expires, confirmed_at = "reserved", \
                self._add_minutes(now, rules["reservation_ttl_minutes"]), None
        reasons = _json(item["reasons_json"])
        reasons = _json(item["reasons_json"])
        reasons.append(self._reason("override_granted" if item["override_flag"]
                                    else "advanced_from_waitlist",
                                    from_state="waitlisted", to_state=state,
                                    displaced=bool(item["displaced_flag"]),
                                    override=bool(item["override_flag"])))
        connection.execute(
            "UPDATE alloc_items SET state=?, expires_at=?, granted_at=COALESCE(granted_at,?), "
            "confirmed_at=COALESCE(confirmed_at,?), released_qty=0, displaced_flag=0, override_flag=0, "
            "reasons_json=?, updated_at=? WHERE item_id=?",
            (state, expires, now, confirmed_at, canonical_json(reasons), now, item["item_id"]),
        )
        if state == "confirmed" and item["confirmed_at"] is None:
            self._create_milestones(connection, item, resource, now)
        append_event(connection, actor_id=actor_id, action="item.advanced",
                     resource_type="allocation_item", resource_id=item["item_id"],
                     detail={"to_state": state, "displaced": bool(item["displaced_flag"]),
                             "override": bool(item["override_flag"]), "score": item["score"]},
                     occurred_at=now)

    def _release_item(self, connection, item, reason_code: str, *, terminal_state: str,
                      actor_id: str, release_qty: int | None = None, **detail: Any) -> int:
        remaining = item["quantity"] - item["fulfilled_qty"] - item["released_qty"]
        release = remaining if release_qty is None else min(release_qty, remaining)
        if release <= 0:
            return 0
        now = self._now()
        released = item["released_qty"] + release
        connection.execute(
            "UPDATE alloc_items SET released_qty=?, state=?, expires_at=NULL, updated_at=? WHERE item_id=?",
            (released, terminal_state, now, item["item_id"]),
        )
        self._append_reason(connection, item, self._reason(reason_code, released=release, **detail))
        append_event(connection, actor_id=actor_id, action="item.released",
                     resource_type="allocation_item", resource_id=item["item_id"],
                     detail={"reason": reason_code, "released": release,
                             "fulfilled_kept": item["fulfilled_qty"], "terminal_state": terminal_state},
                     occurred_at=now)
        return release

    # ------------------------------------------------------------------ 材料、确认、履约

    def supply_material(self, *, actor_id: str, item_id: str, code: str, content: dict[str, Any]):
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            item = self._get_item(connection, item_id)
            if item["state"] not in ("reserved", "waitlisted"):
                raise ConflictError("当前状态不能补充材料")
            team_id = self._team_of_item(connection, item)
            team = connection.execute("SELECT * FROM alloc_teams WHERE team_id=?", (team_id,)).fetchone()
            if actor.role != "admin" and actor.actor_id != team["contact_actor_id"]:
                raise PermissionDenied("只有团队联系人可以补充材料")
            resource = self._load_resource(connection, item["resource_id"])
            if code not in resource.materials:
                raise ValidationError("该材料不在资源要求清单内")
            if not isinstance(content, dict) or not content:
                raise ValidationError("材料内容必须是非空对象")
            from .audit import digest
            content_hash = digest(content)
            now = self._now()
            connection.execute(
                "INSERT INTO alloc_item_materials(item_id,code,content_hash,supplied_by,supplied_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(item_id,code) DO UPDATE SET "
                "content_hash=excluded.content_hash, supplied_by=excluded.supplied_by, supplied_at=excluded.supplied_at",
                (item_id, code, content_hash, actor_id, now),
            )
            append_event(connection, actor_id=actor_id, action="material.supplied",
                         resource_type="allocation_item", resource_id=item_id,
                         detail={"code": code, "content_hash": content_hash}, occurred_at=now)
            result = {"item_id": item_id, "code": code}
            if item["state"] == "reserved":
                item = self._get_item(connection, item_id)
                resource = self._load_resource(connection, item["resource_id"])
                if self._try_confirm(connection, item, resource, actor_id=actor_id):
                    result["confirmed"] = True
                    while self._confirm_ready_reservations(connection, actor_id="system"):
                        pass
            return result

    def confirm_party(self, *, actor_id: str, item_id: str, party: str):
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            item = self._get_item(connection, item_id)
            if item["state"] != "reserved":
                raise ConflictError("只有限时预留中的申请需要确认")
            resource = self._load_resource(connection, item["resource_id"])
            if party not in resource.parties:
                raise ValidationError("该资源不要求此确认方")
            team_id = self._team_of_item(connection, item)
            team = connection.execute("SELECT * FROM alloc_teams WHERE team_id=?", (team_id,)).fetchone()
            provider = connection.execute(
                "SELECT * FROM alloc_providers WHERE provider_id=?", (resource.provider_id,)
            ).fetchone()
            if party == "team" and actor.role != "admin" and actor.actor_id != team["contact_actor_id"]:
                raise PermissionDenied("团队确认必须由团队联系人完成")
            if party == "provider" and actor.role != "admin" and actor.actor_id != provider["contact_actor_id"]:
                raise PermissionDenied("提供方确认必须由提供方联系人完成")
            if party == "operator":
                self._require(actor, "admin", "operator")
            now = self._now()
            connection.execute(
                "INSERT OR IGNORE INTO alloc_item_confirmations(item_id,party,actor_id,confirmed_at) "
                "VALUES(?,?,?,?)",
                (item_id, party, actor_id, now),
            )
            append_event(connection, actor_id=actor_id, action="party.confirmed",
                         resource_type="allocation_item", resource_id=item_id,
                         detail={"party": party}, occurred_at=now)
            item = self._get_item(connection, item_id)
            confirmed = self._try_confirm(connection, item, resource, actor_id=actor_id)
            if confirmed:
                # 前置确认后，依赖它且材料/确认齐备的预留应在同一事务内链式转正式占用
                while self._confirm_ready_reservations(connection, actor_id="system"):
                    pass
            return {"item_id": item_id, "party": party, "confirmed": confirmed}

    def _requirements_complete(self, connection, item, resource: Resource, *, check_prerequisite: bool) -> bool:
        materials = {r["code"] for r in connection.execute(
            "SELECT code FROM alloc_item_materials WHERE item_id=?", (item["item_id"],)
        ).fetchall()}
        if not set(resource.materials).issubset(materials):
            return False
        parties = {r["party"] for r in connection.execute(
            "SELECT party FROM alloc_item_confirmations WHERE item_id=?", (item["item_id"],)
        ).fetchall()}
        if not set(resource.parties).issubset(parties):
            return False
        if check_prerequisite:
            app = connection.execute("SELECT * FROM alloc_applications WHERE application_id=?",
                                     (item["application_id"],)).fetchone()
            for prereq in resource.requires:
                state = self._team_resource_state(connection, app["team_id"], prereq)
                if state not in ("confirmed", "fulfilled"):
                    return False
        return True

    def _try_confirm(self, connection, item, resource: Resource, *, actor_id: str) -> bool:
        if item["state"] != "reserved":
            return False
        if not self._requirements_complete(connection, item, resource, check_prerequisite=True):
            return False
        now = self._now()
        connection.execute(
            "UPDATE alloc_items SET state='confirmed', expires_at=NULL, confirmed_at=?, updated_at=? "
            "WHERE item_id=?",
            (now, now, item["item_id"]),
        )
        self._create_milestones(connection, item, resource, now)
        self._append_reason(connection, item, self._reason("confirmed"))
        append_event(connection, actor_id=actor_id, action="item.confirmed",
                     resource_type="allocation_item", resource_id=item["item_id"],
                     detail={"resource_id": resource.resource_id}, occurred_at=now)
        return True

    def _create_milestones(self, connection, item, resource: Resource, confirmed_at: str) -> None:
        for milestone in resource.milestones:
            due = self._add_days(confirmed_at, int(milestone["due_days"]))
            connection.execute(
                "INSERT OR IGNORE INTO alloc_item_milestones(item_id,code,due_at) VALUES(?,?,?)",
                (item["item_id"], milestone["code"], due),
            )

    def abandon(self, *, request_id: str, actor_id: str, item_id: str):
        payload = {"actor_id": actor_id, "item_id": item_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            item = self._get_item(connection, item_id)
            if item["state"] not in ("reserved", "confirmed"):
                raise ConflictError("只有预留中或已占用的申请可以放弃")
            team_id = self._team_of_item(connection, item)
            team = connection.execute("SELECT * FROM alloc_teams WHERE team_id=?", (team_id,)).fetchone()
            if actor.role != "admin" and actor.actor_id != team["contact_actor_id"]:
                raise PermissionDenied("只有团队联系人可以放弃")

            def create():
                self._release_item(connection, item, "abandoned", terminal_state="abandoned",
                                   actor_id=actor_id)
                result = self._sweep_locked(connection, actor_id=actor_id)
                return "allocation_item", item_id, {"item_id": item_id, "state": "abandoned",
                                                    "advanced": result["advanced"],
                                                    "confirmed": result["confirmed"]}

            return self._idempotent(connection, request_id=request_id, action="abandon_item",
                                    payload=payload, create=create)

    def record_milestone(self, *, actor_id: str, item_id: str, code: str, passed: bool, note: str = ""):
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            item = self._get_item(connection, item_id)
            if item["state"] not in ("reserved", "confirmed", "fulfilled"):
                raise ConflictError("当前状态不能登记里程碑")
            row = connection.execute(
                "SELECT * FROM alloc_item_milestones WHERE item_id=? AND code=?", (item_id, code)
            ).fetchone()
            if row is None:
                raise NotFoundError("里程碑不存在")
            now = self._now()
            connection.execute(
                "UPDATE alloc_item_milestones SET passed=?, decided_by=?, decided_at=?, note=? "
                "WHERE item_id=? AND code=?",
                (1 if passed else 0, actor_id, now, self._text(note, "note", 500) if note else "",
                 item_id, code),
            )
            result: dict[str, Any] = {"item_id": item_id, "code": code, "passed": passed}
            if not passed:
                remaining = item["quantity"] - item["fulfilled_qty"] - item["released_qty"]
                if remaining > 0:
                    self._release_item(connection, item, "milestone_failed", terminal_state="released",
                                       actor_id=actor_id, milestone=code)
                    sweep = self._sweep_locked(connection, actor_id=actor_id)
                    result["advanced"] = sweep["advanced"]
                    result["confirmed"] = sweep["confirmed"]
                else:
                    # 已全部兑现：服务不得回收，仅留痕
                    self._append_reason(connection, item, self._reason("milestone_failed_after_fulfilled",
                                                                      milestone=code))
            append_event(connection, actor_id=actor_id, action="milestone.decided",
                         resource_type="allocation_item", resource_id=item_id,
                         detail={"code": code, "passed": passed}, occurred_at=now)
            return result

    def record_fulfillment(self, *, request_id: str, actor_id: str, item_id: str,
                           quantity: int, note: str = ""):
        payload = {"actor_id": actor_id, "item_id": item_id, "quantity": quantity, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            item = self._get_item(connection, item_id)
            if item["state"] != "confirmed":
                raise ConflictError("只有正式占用的申请可以登记履约")
            if not isinstance(quantity, int) or quantity <= 0:
                raise ValidationError("quantity 必须是正整数")
            remaining = item["quantity"] - item["fulfilled_qty"] - item["released_qty"]
            if quantity > remaining:
                raise ValidationError("履约数量不能超过未兑现余量")
            resource = self._load_resource(connection, item["resource_id"])
            provider = connection.execute(
                "SELECT * FROM alloc_providers WHERE provider_id=?", (resource.provider_id,)
            ).fetchone()
            if actor.role != "admin" and actor.actor_id != provider["contact_actor_id"]:
                raise PermissionDenied("只有该资源的提供方联系人可以登记履约")
            note = self._text(note, "note", 500) if note else ""

            def create():
                fulfillment_id = uuid.uuid4().hex
                now = self._now()
                connection.execute(
                    "INSERT INTO alloc_fulfillments(fulfillment_id,item_id,quantity,note,delivered_by,delivered_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (fulfillment_id, item_id, quantity, note, actor_id, now),
                )
                fulfilled = item["fulfilled_qty"] + quantity
                state = "fulfilled" if fulfilled + item["released_qty"] >= item["quantity"] else "confirmed"
                connection.execute(
                    "UPDATE alloc_items SET fulfilled_qty=?, state=?, updated_at=? WHERE item_id=?",
                    (fulfilled, state, now, item_id),
                )
                append_event(connection, actor_id=actor_id, action="item.fulfilled",
                             resource_type="allocation_item", resource_id=item_id,
                             detail={"fulfillment_id": fulfillment_id, "quantity": quantity,
                                     "fulfilled_qty": fulfilled, "state": state}, occurred_at=now)
                return "fulfillment", fulfillment_id, {"fulfillment_id": fulfillment_id,
                                                       "item_id": item_id, "quantity": quantity,
                                                       "state": state}

            return self._idempotent(connection, request_id=request_id, action="record_fulfillment",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 提供方容量

    def adjust_capacity(self, *, request_id: str, actor_id: str, window_id: str,
                        delta: int, reason: str):
        """提供方调整承诺容量；不足时挤出未履约的最低优先级占用，已履约部分不受影响。"""

        payload = {"actor_id": actor_id, "window_id": window_id, "delta": delta, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "provider")
            window = self._load_window(connection, window_id)
            resource = self._load_resource(connection, window["resource_id"])
            if actor.role == "provider":
                provider = connection.execute(
                    "SELECT * FROM alloc_providers WHERE provider_id=?", (resource.provider_id,)
                ).fetchone()
                if actor.actor_id != provider["contact_actor_id"]:
                    raise PermissionDenied("提供方只能调整自己的资源时段")
            if not isinstance(delta, int) or delta == 0:
                raise ValidationError("delta 必须是非零整数")
            reason = self._text(reason, "reason", 500)
            current = self._window_capacity(connection, window_id)
            new_capacity = current + delta
            if new_capacity < 0:
                raise ValidationError("调整后容量不能为负")
            fulfilled_floor = self._window_fulfilled(connection, window_id)
            if new_capacity < fulfilled_floor:
                raise ValidationError("容量不能低于已履约数量：已兑现服务不得回收")

            def create():
                commitment_id = uuid.uuid4().hex
                now = self._now()
                connection.execute(
                    "INSERT INTO alloc_window_commitments(commitment_id,window_id,capacity_delta,reason,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (commitment_id, window_id, delta, reason, actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="capacity.adjusted",
                             resource_type="window", resource_id=window_id,
                             detail={"delta": delta, "new_capacity": new_capacity, "reason": reason},
                             occurred_at=now)
                displaced: list[str] = []
                if delta < 0:
                    # 先挤出预留，再按得分从低到高挤出已占用项目的未履约部分
                    victims = connection.execute(
                        "SELECT * FROM alloc_items WHERE window_id=? AND state IN ('reserved','confirmed') "
                        "ORDER BY CASE state WHEN 'reserved' THEN 0 ELSE 1 END, score, created_at DESC, item_id",
                        (window_id,),
                    ).fetchall()
                    for victim in victims:
                        if self._window_consumed(connection, window_id) <= new_capacity:
                            break
                        remaining = victim["quantity"] - victim["fulfilled_qty"] - victim["released_qty"]
                        if remaining <= 0:
                            continue
                        displaced.append(victim["item_id"])
                        now2 = self._now()
                        reasons = _json(victim["reasons_json"])
                        reasons.append(self._reason("provider_capacity_reduced", new_capacity=new_capacity))
                        # 不增加 released_qty：整单回到候补，仅 fulfilled_qty 永久占位；
                        # 重新获配时竞争未交付余量。
                        connection.execute(
                            "UPDATE alloc_items SET state='waitlisted', expires_at=NULL, "
                            "confirmed_at=NULL, displaced_flag=1, reasons_json=?, updated_at=? WHERE item_id=?",
                            (canonical_json(reasons), now2, victim["item_id"]),
                        )
                        append_event(connection, actor_id=actor_id, action="item.reassigned_waitlist",
                                     resource_type="allocation_item", resource_id=victim["item_id"],
                                     detail={"reason": "provider_capacity_reduced", "released": remaining,
                                             "fulfilled_kept": victim["fulfilled_qty"],
                                             "new_capacity": new_capacity}, occurred_at=now2)
                sweep = self._sweep_locked(connection, actor_id=actor_id)
                return "window", window_id, {"window_id": window_id, "capacity": new_capacity,
                                             "displaced": displaced, "advanced": sweep["advanced"],
                                             "confirmed": sweep["confirmed"]}

            return self._idempotent(connection, request_id=request_id, action="adjust_capacity",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 人工特批（双人）

    def propose_override(self, *, actor_id: str, item_id: str, reason: str):
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            item = self._get_item(connection, item_id)
            if item["state"] != "waitlisted":
                raise ConflictError("只能对候补中项目发起特批")
            reason = self._text(reason, "reason", 500)
            proposal_id = uuid.uuid4().hex
            now = self._now()
            connection.execute(
                "INSERT INTO alloc_overrides(proposal_id,item_id,reason,proposed_by,proposed_at,status) "
                "VALUES(?,?,?,?,?,'pending')",
                (proposal_id, item_id, reason, actor_id, now),
            )
            append_event(connection, actor_id=actor_id, action="override.proposed",
                         resource_type="override", resource_id=proposal_id,
                         detail={"item_id": item_id, "reason": reason}, occurred_at=now)
            return {"proposal_id": proposal_id, "item_id": item_id, "status": "pending"}

    def approve_override(self, *, actor_id: str, proposal_id: str):
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            row = connection.execute("SELECT * FROM alloc_overrides WHERE proposal_id=?",
                                     (proposal_id,)).fetchone()
            if row is None:
                raise NotFoundError("特批提案不存在")
            if row["status"] != "pending":
                return self._override_view(row)
            if actor.actor_id == row["proposed_by"]:
                raise PermissionDenied("双人批准：批准人必须不同于提议人")
            item = self._get_item(connection, row["item_id"])
            if item["state"] != "waitlisted":
                raise ConflictError("项目已不在候补队列，特批提案自动失效")
            resource = self._load_resource(connection, item["resource_id"])
            window = self._load_window(connection, item["window_id"])
            now = self._now()
            if now > window["ends_at"]:
                raise ConflictError("资源时段已结束，无法特批")
            # 物理互斥与先后依赖仍然强制；特批只能越过排队顺序
            app = connection.execute("SELECT * FROM alloc_applications WHERE application_id=?",
                                     (item["application_id"],)).fetchone()
            if resource.mutex_group and self._mutex_active(
                    connection, app["control_group"], resource.mutex_group, exclude_item=item["item_id"]):
                raise ConflictError("互斥资源已被占用，特批不能覆盖物理互斥")
            rules = self._policy(connection, item["policy_version"])
            needed = item["quantity"] - item["fulfilled_qty"] - item["released_qty"]
            group_cap = rules["group_caps"].get(resource.category)
            if group_cap is not None:
                held = self._group_category_held(connection, app["control_group"], resource.category)
                if held + needed > group_cap:
                    raise ConflictError("特批不能突破同一实际控制组的类目占位上限")
            for prereq in resource.requires:
                if self._team_resource_state(connection, app["team_id"], prereq) not in ("reserved", "confirmed", "fulfilled"):
                    raise ConflictError("前置服务尚未获配，特批不能覆盖先后依赖")
            fairness = self._fairness_impact(connection, item)
            capacity = self._window_capacity(connection, item["window_id"])
            consumed = self._window_consumed(connection, item["window_id"])
            grant_now = consumed + needed <= capacity
            fairness["granted_immediately"] = grant_now
            if grant_now:
                complete = self._requirements_complete(connection, item, resource, check_prerequisite=False)
                state = "confirmed" if complete else "reserved"
                expires = None if complete else self._add_minutes(
                    now, self._policy(connection, item["policy_version"])["reservation_ttl_minutes"])
                reasons = _json(item["reasons_json"])
                reasons.append(self._reason("override_granted", proposal_id=proposal_id,
                                            fairness=fairness))
                connection.execute(
                    "UPDATE alloc_items SET state=?, expires_at=?, override_flag=0, displaced_flag=0, "
                    "granted_at=COALESCE(granted_at,?), reasons_json=?, updated_at=? WHERE item_id=?",
                    (state, expires, now, canonical_json(reasons), now, item["item_id"]),
                )
                if state == "confirmed" and item["confirmed_at"] is None:
                    self._create_milestones(connection, item, resource, now)
                append_event(connection, actor_id=actor_id, action="item.override_granted",
                             resource_type="allocation_item", resource_id=item["item_id"],
                             detail={"proposal_id": proposal_id, "state": state, "fairness": fairness},
                             occurred_at=now)
            else:
                # 容量不足：批准的特排到候补最前，释放容量时优先兑现
                connection.execute(
                    "UPDATE alloc_items SET override_flag=1, updated_at=? WHERE item_id=?",
                    (now, item["item_id"]),
                )
                append_event(connection, actor_id=actor_id, action="override.queued",
                             resource_type="allocation_item", resource_id=item["item_id"],
                             detail={"proposal_id": proposal_id, "fairness": fairness}, occurred_at=now)
            connection.execute(
                "UPDATE alloc_overrides SET approver_id=?, approved_at=?, status=?, fairness_json=? "
                "WHERE proposal_id=?",
                (actor_id, now, "approved" if grant_now else "approved_pending_capacity",
                 canonical_json(fairness), proposal_id),
            )
            append_event(connection, actor_id=actor_id, action="override.approved",
                         resource_type="override", resource_id=proposal_id,
                         detail={"item_id": item["item_id"], "granted_immediately": grant_now,
                                 "fairness": fairness}, occurred_at=now)
            view = self._override_view(connection.execute(
                "SELECT * FROM alloc_overrides WHERE proposal_id=?", (proposal_id,)).fetchone())
            if grant_now:
                self._advance_waitlists(connection, actor_id=actor_id)
            return view

    def _fairness_impact(self, connection, item) -> dict[str, Any]:
        """量化特批越过的候补：数量、团队、得分差。"""

        peers = connection.execute(
            "SELECT i.* FROM alloc_items i WHERE i.window_id=? AND i.state='waitlisted' AND i.item_id<>? "
            "ORDER BY i.override_flag DESC, i.displaced_flag DESC, i.score DESC, i.created_at, i.item_id",
            (item["window_id"], item["item_id"]),
        ).fetchall()
        jumped_ids = [p["item_id"] for p in peers]
        teams = []
        for peer in peers:
            team_id = connection.execute(
                "SELECT team_id FROM alloc_applications WHERE application_id=?",
                (peer["application_id"],),
            ).fetchone()["team_id"]
            if team_id not in teams:
                teams.append(team_id)
        head_score = peers[0]["score"] if peers else item["score"]
        return {
            "jumped_count": len(jumped_ids),
            "jumped_item_ids": jumped_ids,
            "jumped_team_ids": teams,
            "target_score": item["score"],
            "head_score": head_score,
            "score_gap": round(head_score - item["score"], 3),
            "policy_version": item["policy_version"],
        }

    def _override_view(self, row) -> dict[str, Any]:
        return {"proposal_id": row["proposal_id"], "item_id": row["item_id"], "reason": row["reason"],
                "proposed_by": row["proposed_by"], "proposed_at": row["proposed_at"],
                "approver_id": row["approver_id"], "approved_at": row["approved_at"],
                "status": row["status"], "fairness": _json(row["fairness_json"])}

    # ------------------------------------------------------------------ 查询

    def list_policies(self) -> list[dict[str, Any]]:
        import json
        rows = self.database.connection.execute(
            "SELECT * FROM alloc_policies ORDER BY policy_version"
        ).fetchall()
        return [{"policy_version": r["policy_version"], "rules": json.loads(r["rules_json"]),
                 "active": bool(r["active"]), "created_by": r["created_by"],
                 "created_at": r["created_at"]} for r in rows]

    def list_windows(self, *, actor_id: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        provider_id = None
        if actor_id is not None:
            actor = self._actor(connection, actor_id)
            if actor.role == "provider":
                row = connection.execute(
                    "SELECT provider_id FROM alloc_providers WHERE contact_actor_id=?", (actor_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("提供方档案不存在")
                provider_id = row["provider_id"]
        result = []
        query = "SELECT w.* FROM alloc_windows w"
        parameters: list[Any] = []
        if provider_id is not None:
            query += " JOIN alloc_resources r ON r.resource_id=w.resource_id WHERE r.provider_id=?"
            parameters.append(provider_id)
        query += " ORDER BY w.starts_at"
        for row in connection.execute(query, parameters):
            result.append({"window_id": row["window_id"], "resource_id": row["resource_id"],
                           "starts_at": row["starts_at"], "ends_at": row["ends_at"],
                           "capacity": self._window_capacity(connection, row["window_id"]),
                           "consumed": self._window_consumed(connection, row["window_id"])})
        return result

    def waitlist(self, *, window_id: str, actor_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer", "auditor", "provider")
            window = self._load_window(connection, window_id)
            if actor.role == "provider":
                resource = self._load_resource(connection, window["resource_id"])
                provider = connection.execute(
                    "SELECT * FROM alloc_providers WHERE provider_id=?", (resource.provider_id,)
                ).fetchone()
                if actor.actor_id != provider["contact_actor_id"]:
                    raise PermissionDenied("提供方只能查看自己资源的候补队列")
            self._sweep_locked(connection)
            rows = connection.execute(
                "SELECT i.* FROM alloc_items i WHERE i.window_id=? AND i.state='waitlisted' "
                "ORDER BY i.override_flag DESC, i.displaced_flag DESC, i.score DESC, i.created_at, i.item_id",
                (window_id,),
            ).fetchall()
            items = []
            for rank, row in enumerate(rows, start=1):
                view = self._item_view(connection, row)
                view["rank"] = rank
                items.append(view)
            return {"window_id": window_id, "items": items}

    def get_application(self, *, application_id: str, actor_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            app = connection.execute("SELECT * FROM alloc_applications WHERE application_id=?",
                                     (application_id,)).fetchone()
            if app is None:
                raise NotFoundError("申请不存在")
            team = connection.execute("SELECT * FROM alloc_teams WHERE team_id=?", (app["team_id"],)).fetchone()
            if actor.role not in ("admin", "operator", "reviewer", "auditor") \
                    and actor.actor_id != team["contact_actor_id"]:
                raise PermissionDenied("只能查看本团队的申请")
            self._sweep_locked(connection)
            app = connection.execute("SELECT * FROM alloc_applications WHERE application_id=?",
                                     (application_id,)).fetchone()
            items = [self._item_view(connection, r) for r in connection.execute(
                "SELECT * FROM alloc_items WHERE application_id=? ORDER BY rowid",
                (application_id,)).fetchall()]
            for view in items:
                if view["state"] == "waitlisted":
                    view["rank"] = self._waitlist_rank(connection, view["item_id"])
            summary: dict[str, int] = {}
            for view in items:
                summary[view["state"]] = summary.get(view["state"], 0) + 1
            return {"application_id": application_id, "award_id": app["award_id"],
                    "team_id": app["team_id"], "control_group": app["control_group"],
                    "policy_version": app["policy_version"], "created_by": app["created_by"],
                    "created_at": app["created_at"], "summary": summary, "items": items}

    def _waitlist_rank(self, connection, item_id: str) -> int | None:
        rows = connection.execute(
            "SELECT item_id FROM alloc_items WHERE state='waitlisted' AND window_id="
            "(SELECT window_id FROM alloc_items WHERE item_id=?) "
            "ORDER BY override_flag DESC, displaced_flag DESC, score DESC, created_at, item_id",
            (item_id,),
        ).fetchall()
        for index, row in enumerate(rows, start=1):
            if row["item_id"] == item_id:
                return index
        return None

    def _item_view(self, connection, row) -> dict[str, Any]:
        import json
        resource = self._load_resource(connection, row["resource_id"])
        materials = [r["code"] for r in connection.execute(
            "SELECT code FROM alloc_item_materials WHERE item_id=? ORDER BY code", (row["item_id"],)
        ).fetchall()]
        parties = [{"party": r["party"], "actor_id": r["actor_id"], "confirmed_at": r["confirmed_at"]}
                   for r in connection.execute(
                       "SELECT * FROM alloc_item_confirmations WHERE item_id=? ORDER BY party",
                       (row["item_id"],)).fetchall()]
        milestones = [{"code": r["code"], "due_at": r["due_at"], "passed": r["passed"],
                       "decided_at": r["decided_at"]} for r in connection.execute(
            "SELECT * FROM alloc_item_milestones WHERE item_id=? ORDER BY due_at, code",
            (row["item_id"],)).fetchall()]
        return {
            "item_id": row["item_id"], "resource_id": row["resource_id"],
            "provider_id": resource.provider_id, "category": resource.category,
            "window_id": row["window_id"], "quantity": row["quantity"],
            "state": row["state"], "policy_version": row["policy_version"],
            "score": row["score"], "score_components": json.loads(row["score_components_json"]),
            "reasons": json.loads(row["reasons_json"]),
            "expires_at": row["expires_at"], "confirmed_at": row["confirmed_at"],
            "fulfilled_qty": row["fulfilled_qty"], "released_qty": row["released_qty"],
            "remaining_qty": row["quantity"] - row["fulfilled_qty"] - row["released_qty"],
            "displaced": bool(row["displaced_flag"]), "override_priority": bool(row["override_flag"]),
            "required_materials": list(resource.materials), "supplied_materials": materials,
            "required_parties": list(resource.parties), "confirmations": parties,
            "milestones": milestones, "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def provider_deliveries(self, *, actor_id: str) -> dict[str, Any]:
        """提供方视角的交付清单：待确认、待交付与里程碑。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "provider")
            self._sweep_locked(connection)
            provider = None
            if actor.role == "provider":
                provider = connection.execute(
                    "SELECT * FROM alloc_providers WHERE contact_actor_id=?", (actor_id,)
                ).fetchone()
                if provider is None:
                    raise NotFoundError("提供方档案不存在")
            sql = (
                "SELECT i.* FROM alloc_items i JOIN alloc_resources r ON r.resource_id=i.resource_id "
                "WHERE i.state IN ('reserved','confirmed') "
            )
            parameters: list[Any] = []
            if provider is not None:
                sql += "AND r.provider_id=? "
                parameters.append(provider["provider_id"])
            sql += "ORDER BY i.confirmed_at, i.created_at"
            deliveries = []
            for row in connection.execute(sql, parameters).fetchall():
                view = self._item_view(connection, row)
                resource = self._load_resource(connection, row["resource_id"])
                window = self._load_window(connection, row["window_id"])
                deliver_by = None
                if row["confirmed_at"] and resource.fulfillment_due_days is not None:
                    deliver_by = self._add_days(row["confirmed_at"], resource.fulfillment_due_days)
                view["deliver_by"] = deliver_by or window["ends_at"]
                deliveries.append(view)
            return {"provider_id": provider["provider_id"] if provider else None, "items": deliveries}

    def simulate_policy(self, *, actor_id: str, item_id: str, policy_version: str) -> dict[str, Any]:
        """用另一政策版本重算该明细的得分与硬资格，只读比较，不改写既往结果。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer", "auditor")
            row = self._get_item(connection, item_id)
            facts = _json(row["facts_json"])
            resource = self._load_resource(connection, row["resource_id"])
            target = self._policy(connection, policy_version)
            actual_rules = self._policy(connection, row["policy_version"])
            if facts["award_level"] not in target["level_rank"]:
                raise ValidationError("目标政策缺少该获奖等级定义")
            target_rank = target["level_rank"][facts["award_level"]]
            target_score, target_components = self._score(
                target, level_rank=target_rank, maturity=facts["maturity_score"],
                urgency=facts["urgency"])
            actual_score, _ = self._score(
                actual_rules, level_rank=facts["level_rank"],
                maturity=facts["maturity_score"], urgency=facts["urgency"])
            target_eligible = target_rank >= resource.min_level_rank \
                and facts["maturity_score"] >= resource.min_maturity
            actual_eligible = facts["level_rank"] >= resource.min_level_rank \
                and facts["maturity_score"] >= resource.min_maturity
            return {
                "item_id": item_id, "actual": {"policy_version": row["policy_version"],
                                               "score": actual_score, "eligible": actual_eligible,
                                               "state": row["state"]},
                "simulated": {"policy_version": policy_version, "score": target_score,
                              "components": target_components, "level_rank": target_rank,
                              "eligible": target_eligible},
                "score_delta": round(target_score - actual_score, 3),
                "eligibility_changed": target_eligible != actual_eligible,
                "note": "容量、互斥与依赖为动态状态，仅比较得分与硬资格，既往决策不被改写",
            }

    def get_team(self, team_id: str) -> Team:
        row = self.database.connection.execute("SELECT * FROM alloc_teams WHERE team_id=?", (team_id,)).fetchone()
        if row is None:
            raise NotFoundError("团队不存在")
        return Team(row["team_id"], row["name"], row["contact_actor_id"],
                    self._control_group(self.database.connection, team_id))

    def get_award(self, award_id: str) -> Award:
        row = self.database.connection.execute("SELECT * FROM alloc_awards WHERE award_id=?", (award_id,)).fetchone()
        if row is None:
            raise NotFoundError("获奖作品不存在")
        return Award(row["award_id"], row["team_id"], row["award_level"], row["title"],
                     row["maturity_score"])

    def get_window(self, window_id: str) -> ResourceWindow:
        row = self.database.connection.execute("SELECT * FROM alloc_windows WHERE window_id=?", (window_id,)).fetchone()
        if row is None:
            raise NotFoundError("资源时段不存在")
        return ResourceWindow(row["window_id"], row["resource_id"], row["starts_at"], row["ends_at"],
                              self._window_capacity(self.database.connection, window_id),
                              self._window_consumed(self.database.connection, window_id))


def _json(value: str) -> Any:
    import json
    return json.loads(value)


def _hash_rules(rules: dict[str, Any]) -> str:
    from .audit import digest
    return digest(rules)


def _validate_rules(rules: Any) -> dict[str, Any]:
    if not isinstance(rules, dict):
        raise ValidationError("rules 必须是对象")
    merged = {
        "reservation_ttl_minutes": DEFAULT_RULES["reservation_ttl_minutes"],
        "weights": dict(DEFAULT_RULES["weights"]),
        "level_rank": dict(DEFAULT_RULES["level_rank"]),
        "group_caps": dict(DEFAULT_RULES["group_caps"]),
    }
    if "reservation_ttl_minutes" in rules:
        ttl = rules["reservation_ttl_minutes"]
        if not isinstance(ttl, int) or ttl <= 0:
            raise ValidationError("reservation_ttl_minutes 必须是正整数")
        merged["reservation_ttl_minutes"] = ttl
    if "weights" in rules:
        weights = rules["weights"]
        if not isinstance(weights, dict):
            raise ValidationError("weights 必须是对象")
        for key in ("award_level", "maturity", "urgency"):
            value = weights.get(key, merged["weights"][key])
            if not isinstance(value, (int, float)) or value < 0:
                raise ValidationError(f"权重 {key} 必须是非负数")
            merged["weights"][key] = value
    if "level_rank" in rules:
        ranks = rules["level_rank"]
        if not isinstance(ranks, dict) or set(ranks) != set(DEFAULT_RULES["level_rank"]):
            raise ValidationError("level_rank 必须包含全部获奖等级")
        if not all(isinstance(v, int) and v > 0 for v in ranks.values()):
            raise ValidationError("等级分值必须是正整数")
        merged["level_rank"] = ranks
    if "group_caps" in rules:
        caps = rules["group_caps"]
        if not isinstance(caps, dict):
            raise ValidationError("group_caps 必须是对象")
        for category, value in caps.items():
            if category not in CATEGORIES or not isinstance(value, int) or value <= 0:
                raise ValidationError(f"类目 {category} 的上限必须是正整数")
            merged["group_caps"][category] = value
    return merged
