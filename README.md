# 分配获奖服务资源包协作基础服务

本项目提供文化创意赛事与成果转化业务共享的服务端基础能力：项目机构、业务节点、操作者和结构化参考资料的登记，以及在此基础上构建的**获奖服务资源分配平台**（空间、展陈、融资对接、宣传、渠道上架五类服务）。系统内置角色权限、请求幂等、SQLite 事务与哈希串联审计，全部仅依赖 Python 标准库和 SQLite。

## 目录

- src/creative_program_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由、离线验收；
  - `allocation.py`：获奖服务资源分配（申请决策、限时预留、稳定候补、双人特批、政策版本）；
  - `acceptance_allocation.py`：分配平台端到端离线验收；
- tests/：基础规则、分配决策、事务边界、接口路由和端到端验收测试。

## 分配平台决策模型

- **统一决策因子**：获奖等级、项目成熟度、紧急度按生效政策版本加权打分并随申请快照冻结；
  团队关联关系经并查集归并为“实际控制组”；资源声明容量时段、互斥组、先后依赖、必备材料、多方确认方与里程碑。
- **组合申请**：一次事务内逐条给出 `reserved`（限时预留）、`waitlisted`（候补，含机器可读原因与候补名次）或 `rejected`（硬资格不符）。
- **限时预留转正式占用**：在 TTL 内补齐材料并完成全部确认方确认（前置服务须已正式占用）才转 `confirmed`。
- **稳定候补顺序**：`特批标记 → 被容量缩减挤出标记 → 冻结得分 → 提交时间 → 明细编号`，全部可由 SQL 重放，重启后结果一致；
  放弃、预留逾期、里程碑未达标、提供方缩减容量都在同一事务内释放未兑现部分并推进候补到不动点。
- **已兑现服务不回收**：`fulfilled_qty` 永久占用容量；容量不得调减到已履约数量之下；部分履约后失败只释放余量。
- **反重复占位**：同一实际控制组在每个服务类目上有在占容量上限；互斥资源在组内不可共存。
- **双人特批**：运营/管理员提议后必须由另一名同级或管理员批准；特批不能越过物理互斥、先后依赖和同组上限；
  批准时量化记录越过的候补项目、团队、得分差与政策版本。容量不足时进入候补最前，释放后优先兑现。
- **政策版本**：每个决策快照政策版本、规则哈希与打分明细；新政策只影响后续决策；
  `policy-simulation` 接口只读比较不同版本下的得分与资格，不改写既往结果。
- **可观测性**：申请方通过 API 看到获配/候补/拒绝的具体原因与候补名次；提供方看到仅含自己资源的交付清单与履约期限；
  审计人员可按哈希链核验全部事件并比较政策版本。

## 主要接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /teams` `/providers` `/awards` `/resources` `/windows` | 团队、提供方、获奖作品、资源与时段登记（均支持幂等 `request_id`） |
| `POST /team-relations` | 登记团队关联，用于识别同一实际控制组 |
| `POST /applications` | 提交组合申请，返回各明细的预留/候补/拒绝结果与原因 |
| `GET /applications/{id}` | 申请方或运营查看决策、原因、候补名次、材料/确认/里程碑状态 |
| `GET /waitlist?window_id=` | 查看稳定候补队列（提供方限本资源） |
| `POST /items/{id}/materials` `/confirmations` `/abandon` `/milestones` | 补材料、多方确认、放弃、里程碑判定 |
| `POST /fulfillments` | 提供方登记部分或全部履约 |
| `POST /windows/{id}/capacity` | 提供方承诺增减容量，自动挤出/推进并保护已履约部分 |
| `POST /overrides`、`POST /overrides/{id}/approve` | 双人特批的提议与批准 |
| `POST /policies`、`GET /policies` | 发布/查看政策版本 |
| `GET /items/{id}/policy-simulation?policy_version=v2` | 只读比较政策版本影响 |
| `GET /provider-deliveries` | 提供方交付清单（按提供方隔离） |
| `POST /sweep` | 显式处理预留到期与候补推进（查询接口也会触发，结果幂等） |

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m creative_program_foundation.acceptance
    PYTHONPATH=src python3 -m creative_program_foundation.acceptance_allocation

验收命令在临时 SQLite 数据库中走通完整业务链（含重启一致性），成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m creative_program_foundation.api --database creative_program.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。
