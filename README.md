# 流失文物返还协作台账

面向跨机构文物返还案件的**服务端协作系统**：以不可跳过的状态规则管理线索登记 → 证据封存 → 专家鉴定 → 对外请求 → 对方确认 → 运输交接 → 结案归档的完整链条。采用事件溯源（event sourcing）：所有变更只追加事件，当前状态由事件流重放得到，撤销以冲正事件实现、历史永不抹去，服务重启后可从 SQLite 恢复未完成案件。

## 设计要点

- **不可跳序的状态机**：六阶段严格推进，每步都校验前置条件（材料类别、批准、对方确认等），缺条件返回 `409 out_of_order`。
- **重复送达不重复推进**：外部函件/回执按其外部文号全局去重，重复送达返回 `409 duplicate_event` 并回指首次事件；网络重试可用 `Idempotency-Key`，同键返回 `200 replayed`。
- **争议受限**：争议期间物件冻结，任何推进、批准、冲正都被拒绝；解除后方可继续。
- **职责分离**：关键动作（发出对外请求、实体交接、结案归档）须由具备 approver/reviewer 职责者批准，**原鉴定人即使身兼批准职责也不得批准本案**。
- **决定可追溯**：每次决定固化所依据的材料版本（版本号 + 哈希 + 事件序号）、操作者、职责、UTC 时间。
- **撤销不抹历史**：冲正事件抵消原录入，原事件仍完整保留在监管台账；已被后续决定引用或已越过的阶段节点不可倒改。
- **并发安全**：逐案件锁 + `BEGIN IMMEDIATE` 事务 + 幂等键唯一索引三重保障，并发批准只有一人成功。

## 目录

```
src/relic_case/
  contracts.py   领域契约：阶段 / 材料类别 / 职责 / 外部标识校验
  storage.py     SQLite 事件存储（只追加 + 幂等唯一索引）
  engine.py      状态机、权限、去重、争议冻结、冲正、重放恢复、案件视图与台账
  httpapi.py     HTTP JSON 接口（标准库，零第三方依赖）
  server.py      启动入口
tests/           70 个自动化测试
```

## 运行

需要 Python ≥ 3.11，无第三方依赖。

```bash
# 启动服务（默认 0.0.0.0:8080，数据库 relic_ledger.db）
python3 -m src.relic_case.server --db ./ledger.db --port 8080

# 运行全部测试
python3 -m unittest discover -s tests -v
```

## 接口约定

- 所有请求/响应均为 JSON（`Content-Type: application/json`）。
- 写请求须带头 `X-Actor-Id: <用户ID>`，其职责取自用户目录。
- 可选头 `Idempotency-Key: <外部标识>`（≥8 字符）用于安全重试。

| 方法 | 路径 | 说明 | 所需职责 |
| --- | --- | --- | --- |
| POST | `/v1/users` | 登记工作人员及职责 | — |
| POST | `/v1/cases` | 线索登记、开立案件 | case_officer |
| GET  | `/v1/cases` | 案件列表（阶段/负责人/缺失条件/是否闭合） | — |
| GET  | `/v1/cases/{id}` | 案件当前状态：谁负责、缺什么、链路 | — |
| GET  | `/v1/cases/{id}/ledger` | **按案件导出完整监管记录** | — |
| POST | `/v1/cases/{id}/materials` | 登记材料（支持版本、哈希、外部文号） | 任意登记人员 |
| POST | `/v1/cases/{id}/seal` | 证据封存（须附 seizure_record） | case_officer |
| POST | `/v1/cases/{id}/attribution` | 录入专家鉴定（须附 expert_opinion） | expert |
| POST | `/v1/cases/{id}/approvals` | 复核/批准关键动作 | approver / reviewer |
| POST | `/v1/cases/{id}/request` | 发出对外请求（须附 diplomatic_note + 批准） | case_officer |
| POST | `/v1/cases/{id}/confirmation` | 登记境外对方确认（须回复我方文号） | case_officer / custodian |
| POST | `/v1/cases/{id}/handover` | 运输交接（须附 handover_receipt + 确认 + 批准） | custodian |
| POST | `/v1/cases/{id}/archive` | 结案归档（须批准） | case_officer |
| POST | `/v1/cases/{id}/disputes` | 提出争议（进入受限冻结） | 任意登记人员 |
| POST | `/v1/cases/{id}/disputes/resolve` | 解除争议 | reviewer / case_officer |
| POST | `/v1/cases/{id}/reversals` | 冲正撤销错误录入（历史保留） | case_officer / reviewer |

批准动作通过 body 的 `action` 区分：`issue_request`、`complete_handover`、`archive`。

### 错误响应

```json
{"error": {"code": "out_of_order", "message": "当前阶段为 intake，该操作要求处于 evidence_sealed"}}
```

| HTTP | code | 场景 |
| --- | --- | --- |
| 403 | `forbidden` | 无此职责 / 原鉴定人试图批准 / 缺少操作者头 |
| 404 | `not_found` | 案件或事件不存在 |
| 409 | `out_of_order` | 乱序跳步、缺前置批准、争议冻结中推进 |
| 409 | `duplicate_event` | 外部函件/回执重复送达（附 `original_event_seq`） |
| 400 | `missing_*` / `invalid_input` | 材料类别缺失、文号不匹配、格式错误等 |

幂等重试（同 `Idempotency-Key`）返回 `200`，body 含 `"replayed": true` 与首次事件序号。

### 案件状态视图能回答的三个问题

`GET /v1/cases/{id}` 返回：

- **当前由谁负责**：`responsible.role` 与候选成员（等待境外机关时显式标注）。
- **还缺什么条件**：`missing_conditions`（条件码、说明、所需职责），例如 `counterpart_confirmation`、`handover_approval`。
- **交接链是否闭合**：`chain.complete`，并逐环列出证据封存、专家鉴定、对外请求、对方确认、交接回执、结案归档的事件序号，以及确认函文号与请求文号是否对应（`refs_match`）。

### 监管记录导出

`GET /v1/cases/{id}/ledger` 在状态视图之外额外提供：

- `decisions`：每个关键决定的操作者、职责、时间、**材料版本依据快照**，以及是否已被冲正；
- `event_log`：从开案起的全部事件（含被冲正事件与冲正事件本身），任何历史都不缺失。

## 示例

```bash
curl -sX POST localhost:8080/v1/users \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"officer1","agency":"海关","roles":["case_officer"]}'

curl -sX POST localhost:8080/v1/cases \
  -H 'Content-Type: application/json' -H 'X-Actor-Id: officer1' \
  -d '{"case_id":"CASE-2026-0001","item":{"name":"青铜鼎"}}'
```
