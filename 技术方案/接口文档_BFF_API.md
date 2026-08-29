# 周末去哪儿 BFF 接口文档（v4）

> 版本：v4（回合状态机与任务生命周期重构后）
> 更新日期：2026-07-31
> 服务入口：`http://127.0.0.1:8000`（本地开发）；静态前端演示页 `GET /ui/`
> 代码位置：`src/wheretogo/bff/agent_api.py`（v4 Agent API）、`src/wheretogo/bff/app.py`（规划/回填/协作/兼容）

---

## 0. 总览与通用约定

### 0.1 两套 API 面

| 面 | 前缀 | 用途 | 前端是否使用 |
|---|---|---|---|
| **v4 Agent API** | `/agent/*` | 对话回合、Run 事件订阅、工作区恢复（**主用**） | ✅ 新前端只用这套 |
| 规划/兼容 API | `/plans/*`、`/invite/*` | 显式规划流、SSE 出图、回填、多人协作、ICS；旧 SSE 端点保留为兼容层 | 部分（回填/协作/ICS 无浏览器 UI，走 API） |

v4 的执行模型：一次用户消息 = 一个持久化 **Turn**；需要外部工作时在事务内创建 **Run** 并投递 Outbox，由**独立 Worker 进程**执行，前端通过 SSE 订阅 Run 事件。浏览器关闭不影响任务执行。

### 0.2 通用约定

- 请求/响应体默认 `application/json; charset=utf-8`。
- 时间统一 ISO 8601（业务时区 Asia/Shanghai，落库 UTC）。
- `plan_id` 为数字字符串；`turn_id`/`run_id`/`clarification_id` 为 UUID。
- SSE 响应为 `text/event-stream`，帧格式 `event: <type>\ndata: <json>\n\n`。
- 幂等：v4 Turn 端点支持 `Idempotency-Key` 请求头（见 1.1）。

### 0.3 统一错误约定

| HTTP | 场景 | 响应体 |
|---|---|---|
| 404 | plan/run/turn/邀请不存在 | `{"detail": "plan 不存在"}` |
| 409 | 同一 plan 有研究在跑（锁冲突） | `{"detail": "该方案正在生成中……"}` |
| 422 | 请求体校验失败（FastAPI/Pydantic） | `{"detail": [{"loc":...,"msg":...}]}` |
| 500 | Turn 合约校验失败（运行时缺陷，极少） | `{"detail": "回合状态校验失败，此轮未提交；请重发消息。"}` |

v4 回合的**业务失败**不走 HTTP 错误码，而是返回 200/202 且 `turn_status=failed` + `error={code,message,recovery}`（诚实降级，见 §4）。

### 0.4 时延基线（本地实测 / E2E 实测，2026-07-31）

| 类别 | 端点 | 实测时延 | 说明 |
|---|---|---|---|
| 轻量读 | health / metrics / workspace / agent-state / party-aggregate / calendar.ics | **32–375 ms** | 纯 DB 读；首个冷调用略高 |
| 回合创建 | POST `/agent/.../turns`、POST `/plans/{id}/chat` | **约 3–60 s** | **同步**调用一次 LLM 解释（interpret_turn）；随后研究异步跑在 Worker |
| 回填抽取 | POST `/plans/{id}/bookings/import` | **1 s–3 min** | 本地正则即时兜底 + 一次 LLM 抽取增强（有 key 时等待 LLM） |
| 深研出图（SSE） | stream / resume / replan / research-more / Run events | **2–8 min** | 真实 LLM+搜索多轮并行；SSE 全程推进度 |

> 说明：回合创建当前把"语义解释"放在同步请求内，故其时延≈一次 LLM 结构化抽取；真正耗时的检索/核实/合成异步在 Worker 执行、通过事件流反馈。

---

## 1. v4 Agent API（`/agent/*`，主用）

### 1.1 POST `/agent/conversations/{plan_id}/turns` — 提交一轮对话

一次对话回合的原子入口：解释语义 → 动态前置条件解析 → 运行时决定 → 事务内落库 Turn/澄清/Run/Outbox。

- 路径参数：`plan_id` — 数字字符串；传 `"new"` 时自动新建 plan。
- 请求头（可选）：`Idempotency-Key: <uuid>` — 同 `(plan_id, key)` 重复请求返回同一 Turn/Run，不重复建任务。
- 请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `message` | string(1–4000) | 是 | 用户自然语言消息 |
| `idempotency_key` | string | 否 | 备用幂等键（优先用请求头） |

- 响应：`202`（`turn_status=running`）或 `200`（其它终态/需输入）。

```json
{
  "plan_id": "4742",
  "conversation_id": "4742",
  "turn_id": "3af5ef25-...",
  "turn_status": "running",
  "assistant_message": {"content": "已创建研究任务：……我会实时汇报进度……"},
  "run": {
    "id": "8da82cfd-...",
    "status": "queued",
    "type": "research",
    "goal": "……",
    "parent_run_id": null,
    "assumptions": ["按当前规划窗口（默认本周末）安排时间"],
    "events_url": "/agent/runs/8da82cfd-.../events"
  },
  "clarification": {
    "id": "f5df...",
    "blocking": false,
    "question": "如果你告诉我酒店或抵达车站的位置，我还能优化第一段交通。",
    "reason": "市内路线第一段交通的起点（酒店/车站）",
    "requested_facts": [{"name": "start_location", "description": "……"}],
    "assumptions_if_skipped": [],
    "status": "open"
  },
  "error": null,
  "idempotent": false,

  "reply": "……",
  "auto_stream": false,
  "restart_stream": true,
  "ready_to_plan": true
}
```

字段说明：
- `turn_status`：`running` / `needs_input` / `answered` / `partial` / `failed`（Turn 状态机，见 §5.1）。
- `run`：需要外部执行时非空（research/recompose/answer/replan），否则 `null`。`events_url` 用于订阅进度。
- `clarification`：`blocking=true` → 阻塞澄清（配合 `turn_status=needs_input`，问题必须在 UI 显示）；`false` → 非阻塞提示，不阻断执行。
- `error`：仅业务失败时非空，`{code,message,recovery}`。
- `reply/auto_stream/restart_stream/ready_to_plan`：**旧字段适配器**，由新状态派生，仅供过渡观察，新前端不使用。

错误：`404`（数字 plan 不存在）、`500`（合约校验失败，极少）。

### 1.2 GET `/agent/runs/{run_id}/events` — 订阅 Run 事件（SSE）

- 路径参数：`run_id` — UUID。
- 断线续传：请求头 `Last-Event-ID: <sequence>` 或查询参数 `?after=<sequence>`，只推 `sequence` 之后的事件，不丢不重。
- 响应：`text/event-stream`；事件按 `sequence` 单调递增。终态事件 `payload.final=true` 后服务端关闭连接。
- 事件类型（`event:` 字段）：

| type | 含义 | 关键 payload |
|---|---|---|
| `run.status` | 状态变更（queued/running/最终态） | `phase`、`final`、`turn_status`、`error` |
| `research.progress` | 研究进度 | `message`、`completed`/`total`、`phase` |
| `run.result` | 已生成方案（bundle 落库） | `kind`（interrupt/done） |
| `run.error` | 执行出错（不静默） | `message`、`code` |
| `run.node` | 图节点输出（细粒度） | `node` |

单帧示例：

```text
id: 12
event: research.progress
data: {"event_id":"evt_8da82cfd_12","run_id":"8da82cfd-...","sequence":12,"type":"research.progress","phase":"searching","message":"检索完成 2/3 子主题（第1轮）","payload":{"completed":2,"total":3},"created_at":"2026-07-31T..."}
```

错误：`404`（run 不存在）。

### 1.3 GET `/agent/conversations/{plan_id}/workspace` — 恢复完整工作区

页面刷新/断线重连后一次性拉回对话、运行中任务、待澄清、当前方案；有 `active_run` 时前端据 `last_event_id` 续订事件。

- 路径参数：`plan_id`。
- 响应 `200`：

```json
{
  "plan_id": "4742",
  "stage": "await_booking",
  "constraints": { "...": "plan.constraints 快照" },
  "conversation": [{"role":"user|assistant","content":"...","intent":"...","turn_status":"..."}],
  "active_turn": {"id":"...","sequence_no":3,"status":"answered","user_message":"...","visible_reply":"...","run_id":"...","clarification_id":"...","created_at":"...","completed_at":"..."},
  "active_run": {"id":"...","status":"running","type":"research","goal":"...","events_url":"...","heartbeat_at":"...","recent_events":[/* 最近 20 条 */]} ,
  "open_clarifications": [{"id":"...","blocking":true,"question":"...","reason":"...","requested_facts":[...],"status":"open"}],
  "current_plan": { "...": "最新探索版 TripBundle payload（活动/交通/行程草案等）" },
  "research_workspace": {"activities":[...],"itinerary_draft":[...],"plan_ledger":{},"research_context":{}},
  "last_event_id": 17
}
```

- `active_run` 无进行中任务时为 `null`；`current_plan` 无 bundle 时为 `null`。
- 错误：`404`（plan 不存在）。

### 1.4 POST `/agent/runs/{run_id}/cancel` — 取消 Run

- 路径参数：`run_id`。
- 行为：置 `cancel_requested=true`；`queued` 立即取消，`running` 由 Supervisor 在下一检查点收敛。已终态直接返回。
- 响应 `200`：`{"ok":true,"status":"cancelled"}` 或 `{"ok":true,"status":"running","cancel_requested":true}` 或 `{"ok":true,"status":"succeeded","already_terminal":true}`。
- 错误：`404`（run 不存在）。

### 1.5 GET `/agent/turns/{turn_id}/trace` — 回合只读 Trace（仅 dev）

- 仅当 `app_env=dev` 开放，否则 `404`。
- 响应 `200`：`{turn, interpretation, clarification, run, events, generated_at}`——从原始消息 → Interpreter JSON → 前置解析 → Run → 事件的完整链路（脱敏，不含密钥与模型隐藏推理）。
- 用途：开发排障；不用于生产。

### 1.6 GET `/agent/metrics` — 关键业务指标

- 响应 `200`：

```json
{
  "turn_total": 150,
  "turn_silent_terminal_total": 0,
  "promised_without_run_total": 0,
  "hidden_clarification_total": 0,
  "run_start_latency_ms": 0.0,
  "clarification_blocking_rate": 0.191,
  "turn_status_counts": {"answered": 141, "needs_input": 4, "failed": 2, "cancelled": 2, "running": 1},
  "run_status_counts": {"succeeded": 111, "failed": 2, "cancelled": 2, "running": 1}
}
```

- 前三项为**可信度硬 KPI**，目标恒为 `0`（无静默终态 / 无"承诺但无 Run" / 无隐藏阻塞澄清）。

---

## 2. 规划与出图（`/plans/*`，兼容层 + 支撑）

> v4 前端不再直接调 `/stream`、`/research-more`；它们保留为兼容/内部执行入口，SSE 事件格式见 §5.3。

### 2.1 GET `/health`
- 无参。响应 `{"ok": true}`。存活探针。

### 2.2 POST `/plans` — 创建规划
- 请求体：`{"constraints": {}, "party": [], "organizer_user_id": null}`（`party` ≤20；老用户注入历史偏好作缺省）。
- 响应：`{"plan_id":"4742","stream":"/plans/4742/stream"}`。

### 2.3 GET `/plans/{plan_id}/agent-state` — 旧版会话恢复
- 响应：`{plan_id, stage, constraints, conversation:[{role,content,intent}], explore_bundle}`。
- 与 v4 `workspace` 的区别：不含 active_run/open_clarifications/last_event_id。新前端用 `workspace`。

### 2.4 GET `/plans/{plan_id}/stream` — 首轮规划出图（SSE）
- 无请求体。从 `plan.constraints` 跑完整规划图，SSE 推进度/中断/最终 bundle。
- 事件见 §5.3。错误：`404`（plan 不存在）。

### 2.5 POST `/plans/{plan_id}/resume` — 回填后续跑（SSE）
- 请求体：`{"bookings": [ {kind,extracted,confirmed,evidence}, ... ]}`（≤20）。合并 DB 已确认回填后从中断点继续，生成确认版。

### 2.6 POST `/plans/{plan_id}/replan` — 重规划（SSE）
- 请求体：`{"reason": "天气转雨", "from_node": "dining"}`；`from_node` 枚举见 §5.4。从指定节点重跑。

### 2.7 POST `/plans/{plan_id}/research-more` — 研究迭代续流（SSE）
- 无请求体。要求图状态已有待处理研究反馈（否则 `409`）。从 reflect 节点续跑深研回环。

### 2.8 POST `/plans/{plan_id}/revise` — 局部改状态（同步）
- 请求体：`{"values": {...}, "from_node": "timeline"}`。plan 锁内 `update_state` + 续跑；响应 `{"ok":true,"stage":"..."}`。锁冲突 `409`。

### 2.9 POST `/plans/{plan_id}/chat` — 旧版对话（兼容）
- 请求体：`{"message": "...", "memory_ctx": {}}`（message 1–4000，非空）。
- 走旧 handle_turn 链路，返回旧协议 `{intent,action,reply,acts,commands,pending_clarify,auto_stream,restart_stream,ready_to_plan,...}`。
- **新接入请用 §1.1 的 v4 turns 端点**；本端点为既有测试/旧客户端保留。plan 锁超时 `409`。

---

## 3. 回填 / 协作 / 导出（`/plans/*`、`/invite/*`）

### 3.1 POST `/plans/{plan_id}/bookings/import` — 订单回填（BYO Booking）
- 请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `kind` | `train`/`flight`/`hotel`/`manual` | 是 | `manual`=未知类型，由文本自动识别 |
| `input_kind` | `text`/`image`/`link`/`manual` | 否 | 默认 `manual` |
| `raw` | string(≤20000) | 否 | 订单原文；有则本地正则+LLM 抽取 |
| `extracted` | object | 否 | 前端已确认字段，覆盖抽取初稿 |
| `token` | string | 否 | 预留 |

- 响应：`{"plan_id","ready_for_resume":bool,"booking":{kind,input_kind,extracted,confirmed,evidence,...}}`。`ready_for_resume=true` 时前端可调 `/resume`。

### 3.2 POST `/plans/{plan_id}/invites` — 生成匿名邀请
- 请求体：`{"count": 2}`（1–8）。
- 响应：`{"plan_id","invites":[{"token":"...","anon_label":"同伴1"}, ...]}`。

### 3.3 GET `/invite/{token}` — 同伴查看邀请
- 响应：`{"plan_id","anon_label"}`；无效 token `404 {"detail":"邀请无效或已关闭"}`。

### 3.4 POST `/invite/{token}/constraints` — 同伴提交约束（脱敏）
- 请求体：`{origin_area?, earliest_depart?, latest_return?, budget_band?{min,max}, prefer_flight?, accept_flight?, accept_night_train?, interests?[], dietary?[]}`。时间须 ISO 8601；`budget_band` 仅 min/max、非负、min≤max。
- 响应：`{"ok":true,"anon_label":"同伴1"}`；无效 token `404`。

### 3.5 GET `/plans/{plan_id}/party/aggregate` — 多人公平聚合
- 无请求体。聚合规则：最早出发取 max、最晚返回取 min、预算取交集、是否接受飞机/夜车取全体 AND、兴趣/忌口取并集；仅返回聚合值（不暴露个人），并合并进 `plan.constraints`。
- 响应：`{"plan_id","aggregated":{earliest_depart,latest_return,budget_band,accept_flight,accept_night_train,interests,soft_preferences,dietary,party_size}|null,"members":N}`。无同伴时 `aggregated=null, members=0`。

### 3.6 GET `/plans/{plan_id}/calendar.ics` — 日历订阅（ICS）
- 查询参数：`token`（可选，预留校验）。响应 `text/calendar`（RFC5545），恒 `200`（无 bundle 时返回兜底 VCALENDAR）。

---

## 4. 业务失败与降级（v4）

v4 回合失败**不返回 HTTP 5xx**（合约校验缺陷除外），而是 `turn_status` + `error` 表达，保证"每轮都有明确终态、绝不静默"：

| 错误码（`error.code`） | 触发 | 用户可读处置（`recovery`） |
|---|---|---|
| `INTERPRETATION_FAILED` | 语义解释失败 | 换种说法/补充目的地或时间 |
| `RUN_CREATION_FAILED` | 任务创建事务失败 | 重发消息重试（回复**不含**承诺语言） |
| `TOOL_TIMEOUT` / `PARTIAL_EVIDENCE` | Provider 超时/部分证据 | 保留已得结果，`turn_status=partial`，可继续补研 |
| `COMPOSITION_FAILED` | 研究成功但回复合成失败 | 候选保留，可只重试合成 |
| `RUN_STALLED` | 心跳超时/多次重试仍失败 | 重发消息重新发起 |

---

## 5. 数据字典

### 5.1 TurnStatus（回合状态机）
`received → interpreting → {needs_input | running | answered | failed}`；`running → {answered | partial | failed | cancelled}`；`needs_input →(用户回答) interpreting`；`partial → {running | answered}`。终态：answered/failed/cancelled（partial 可继续）。

### 5.2 RunStatus（任务状态）
`queued → running → {waiting_tool | composing} → {succeeded | partial | failed | cancelled}`。RunType：`research`（外部检索核实）/ `recompose`（仅重排已有候选，不搜索）/ `answer`（直接回答）/ `replan`（天气等重规划）。

### 5.3 兼容 SSE 事件（`/plans/*/stream` 等）
`progress`（研究进度）/ `interrupt`（探索版就绪，等回填）/ `node_output`（节点输出）/ `done`（确认版完成）/ `error`（出错降级）。帧格式同 §1.2。

### 5.4 图节点枚举（replan/revise 的 `from_node`）
`parse, discover, research, reflect, transport, await_booking, hotel, mobility, dining, weather, timeline, validate, compose`。

### 5.5 证据六态（卡片字段 `verification_status`）
`confirmed_by_user`（已确认）/ `official_source_confirmed`（官方确认）/ `public_source_observed`（公开来源）/ `estimated`（估算）/ `unknown`（待确认）/ `expired`（已过期）。前端按状态用不同颜色角标，估算值与已确认值视觉可辨。

---

## 6. 端点速查表

| # | 方法 | 路径 | 面 | 类型 |
|---|---|---|---|---|
| 1 | POST | `/agent/conversations/{plan_id}/turns` | v4 | JSON（202/200） |
| 2 | GET | `/agent/runs/{run_id}/events` | v4 | SSE |
| 3 | GET | `/agent/conversations/{plan_id}/workspace` | v4 | JSON |
| 4 | POST | `/agent/runs/{run_id}/cancel` | v4 | JSON |
| 5 | GET | `/agent/turns/{turn_id}/trace` | v4 | JSON（仅 dev） |
| 6 | GET | `/agent/metrics` | v4 | JSON |
| 7 | GET | `/health` | 支撑 | JSON |
| 8 | POST | `/plans` | 规划 | JSON |
| 9 | GET | `/plans/{plan_id}/agent-state` | 兼容 | JSON |
| 10 | GET | `/plans/{plan_id}/stream` | 兼容 | SSE |
| 11 | POST | `/plans/{plan_id}/resume` | 规划 | SSE |
| 12 | POST | `/plans/{plan_id}/replan` | 规划 | SSE |
| 13 | POST | `/plans/{plan_id}/research-more` | 兼容 | SSE |
| 14 | POST | `/plans/{plan_id}/revise` | 规划 | JSON |
| 15 | POST | `/plans/{plan_id}/chat` | 兼容 | JSON |
| 16 | POST | `/plans/{plan_id}/bookings/import` | 回填 | JSON |
| 17 | POST | `/plans/{plan_id}/invites` | 协作 | JSON |
| 18 | GET | `/invite/{token}` | 协作 | JSON |
| 19 | POST | `/invite/{token}/constraints` | 协作 | JSON |
| 20 | GET | `/plans/{plan_id}/party/aggregate` | 协作 | JSON |
| 21 | GET | `/plans/{plan_id}/calendar.ics` | 导出 | ICS |

> 维护提示：新增/修改端点后，请同步更新本文件对应小节与速查表；FastAPI 自带的交互式文档也可在 `GET /docs`（Swagger UI）查看自动生成的 schema。
