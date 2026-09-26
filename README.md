# 流失文物返还协作台账

面向联合工作组的跨机构文物追索协作系统。以事件溯源台账管理线索登记、证据封存、专家鉴定、对外请求、对方确认、运输交接、结案归档的完整返还链条，向各机构系统提供 HTTP JSON 接口与按案件导出的监管记录。

## 设计不变量

- **不可跳步**：每个动作都有允许执行的阶段，错序一律返回 `invalid_transition`。
- **幂等**：外部函件、交接回执以 `external_id` 登记；重复送达返回首次结果（`deduplicated=true`），不重复推进。
- **职责分离**：复核（`approve_attribution`）与批准（`approve_request`、`close_case`）的执行人不得出现在原鉴定参与者名单中；各动作均有固定职责要求。
- **争议受限**：`raise_dispute` 后案件进入受限状态，除 `resolve_dispute` 外的一切变更被拒绝。
- **撤销不抹史**：`void` 以新事件标记被撤销的录入（仅限最新一笔录入类事件），原事件保留在台账与导出中。
- **可恢复**：全部状态由 SQLite 事件流折叠而来，服务重启后自动恢复未完成案件。

## 阶段与动作

```
intake ──seal_evidence──▶ evidence_sealed ──submit_appraisal + approve_attribution──▶
attribution ──draft_request + approve_request──▶ requested ──record_confirmation──▶
confirmed ──record_handover──▶ handover ──close_case──▶ archived
```

| 动作 | 所需职责 | 说明 |
| --- | --- | --- |
| `seal_evidence` | case_officer / custodian | 需至少一份 `seizure_record` 材料 |
| `submit_appraisal` | expert | 生成 `expert_opinion` 材料并记录参与者 |
| `approve_attribution` / `reject_attribution` | reviewer | 不得参与原鉴定 |
| `draft_request` | case_officer | 生成 `diplomatic_note`（可带 `external_id`） |
| `approve_request` | approver | 不得参与原鉴定 |
| `record_confirmation` | case_officer | 必须携带对方函件 `external_id`（幂等） |
| `record_handover` | custodian | 必须携带回执 `external_id`（幂等） |
| `close_case` | approver | 不得参与原鉴定；交接链须闭合 |
| `raise_dispute` | 任意已登记职责 | 进入受限状态 |
| `resolve_dispute` | reviewer / approver | 解除受限状态 |

## HTTP 接口

变更类请求须携带 `X-Actor-Id` 头（操作者须已登记）。错误统一为 `{"error": {"code", "message"}}`，状态码：400 输入不合法 / 401 未登记 / 403 越权 / 404 不存在 / 409 错序·受限·冲突。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/actors` | 登记操作者 `{actor_id, name, org, roles}`（重复登记同资料幂等） |
| GET | `/actors` | 操作者名册 |
| POST | `/cases` | 登记案件 `{title, relic, case_id?}` |
| GET | `/cases` | 案件列表（阶段、责任方、交接链是否闭合） |
| GET | `/cases/{id}` | 案件状态：当前责任方、所缺条件、交接链 |
| POST | `/cases/{id}/materials` | 提交材料 `{kind, summary, uri?, external_id?}` |
| POST | `/cases/{id}/actions/{action}` | 执行上表动作，参数放请求体 |
| POST | `/cases/{id}/void` | 撤销录入 `{event_id, reason}` |
| GET | `/cases/{id}/events` | 完整事件台账 |
| GET | `/cases/{id}/export` | 导出监管记录（状态、材料版本、全部事件、交接链、完整性统计） |

案件状态中的关键字段：

- `responsible`：当前责任方（角色与说明）；
- `pending_conditions`：还缺什么条件（`code` + 中文说明）；
- `handover_chain_closed` / `chain`：交接链是否闭合及请求→确认→回执三环的外部标识。

## 运行

启动服务（数据落盘于 SQLite，重启自动恢复）：

```bash
python3 -m src.relic_case --db relic_case.db --host 127.0.0.1 --port 8080
```

执行测试：

```bash
python3 -m unittest discover -s tests -v
```

执行构建检查：

```bash
python3 -m compileall -q src
```

## 目录

- `src/relic_case/contracts.py`：案件阶段、职责、材料类别、动作与外部标识校验。
- `src/relic_case/events.py`：事件类型与事件结构。
- `src/relic_case/store.py`：SQLite 事件存储与外部标识幂等登记。
- `src/relic_case/service.py`：状态规则、职责分离、幂等、争议受限、撤销与审计投影。
- `src/relic_case/api.py`：HTTP JSON 接口。
- `tests/`：状态机、越权与职责分离、幂等、争议受限、撤销、并发批准、重启恢复、HTTP 端到端测试。
