# 双分支合并统一方案

## 背景与总原则

- 两分支共同基点为 `53b7e1e`（当前 main 内容），merge-base 干净。
- **AI agent 对话与深度研究**：以 `codex/conversation-deep-research-refactor` 为准（v4 回合状态机 + `/agent/*` API + interpreter/respond 语义解释 + research 开放语义重构）。
- **前端**：以 `feat/gomate-dual-mode` 的 `web-v2/` 为准，对话面板全量迁移到 v4 `/agent` API。
- **GoMate 房间后端**（rooms 包、`/rooms/*` API、room_graph）来自 gomate 分支，保持原契约不动。

## 阶段一：建分支与 git 合并

- `git checkout -b feat/unify-agent-dual-mode origin/codex/conversation-deep-research-refactor`
- `git merge origin/feat/gomate-dual-mode`，纯新增文件自动并入：`web-v2/**`、`src/wheretogo/rooms/`、`bff/rooms.py`、`models/room.py`、`orchestration/room_*.py`、`domain/route_design.py`、`domain/timeutil.py`、`tests/test_dd18_rooms.py`、`test_route_design.py`、`test_plan_bundle_endpoint.py` 等。

### 15 个双改文件的冲突解法（统一原则：codex 为基，gomate 增量移植）

| 文件 | 解法 |
|---|---|
| `bff/app.py` | codex 版为基；从 gomate 移入：CORS 中间件、`app.include_router(rooms_router)`、design_itinerary 不触发全量重跑的例外分支。gomate 的对话历史落库弃用（codex 已有更完整实现） |
| `copilot/handle_turn.py` / `nlu.py` | codex 版为基；移植 design_itinerary 意图：`_looks_like_route_design` 规则、ROUTE_TABLE 条目、anchor 抽取（`_extract_anchor_names`）、LLM 意图分类 prompt 增补，接入 codex 的 interpreter 主路径与 `_rule_intent` 前置规则 |
| `research/brief.py` / `service.py` / `supervisor.py` | codex 版为基；移植 A) `scope="local"` 市内模式（brief 加 scope 字段、supervisor 市内子主题改写、`_query_hash` 纳入 scope、`deep_research` 签名加 scope 参数）；B) `_upgrade_verified` 证据态升级（Phase 3 交叉验证通过后只升不降：→ public_source_observed / official_source_confirmed，替换纯 ingest_content 路径，按 gomate 实现为准） |
| `schemas/constraints.py`、`domain/constraints.py`、`orchestration/bundle.py`、`providers/llm.py`、`models/__init__.py` | 叠加型合并：codex 为基，gomate 的新增字段/导出（Room 模型、route_design 相关）并入 |
| `.env.example` | codex 版为基（已重排），补 gomate 的 embedding 模型注释 |
| `tests/test_dd17_deep_research.py`、`test_iterative_research.py`、`test_regression_v3_fixes.py` | codex 版为基，gomate 新增用例逐条移植（涉及 scope=local 与证据升级的保留，被 v4 重构取代的断言按 codex 语义修正） |

### 迁移版本号冲突

- 两分支都有 `0006`（codex: `0006_agent_turn_run_lifecycle`；gomate: `0006_gomate_rooms`）。
- 保留 codex 的 0006；gomate 的改名为 `migrations/versions/0007_gomate_rooms.py`，`revision="0007"`、`down_revision="0006"`。

## 阶段二：后端集成修复

- `orchestration/room_nodes.py` 中 `deep_research(..., scope="local")` 调用对齐 codex 新签名（阶段一已给 service 加回 scope 参数，此处验证连通）。
- 确认 rooms 推荐流（room_graph）与 codex 重构后的 `RetrievalService`、`providers` 接口兼容（gomate 对 `retrieval/providers.py`、`retrieval/service.py` 的修改无冲突，自动并入）。
- design_itinerary 意图在 codex 的 `respond.py` 输出契约中补 `route_plan` 字段透传（前端 RoutePlanCard 依赖）。

## 阶段三：web-v2 对话面板全量迁移到 v4 /agent API

参照 codex 分支 `web/index.html` 的交互模型（其已完整对接 v4）与 `技术方案/接口文档_BFF_API.md`：

- `web-v2/lib/api.ts`：新增 v4 client——`POST /agent/conversations/{plan_id}/turns`（携带 Idempotency-Key，202=RUNNING）、`GET /agent/conversations/{plan_id}/workspace`、`POST /agent/runs/{run_id}/cancel`、`GET /agent/turns/{turn_id}/trace`。
- `web-v2/lib/sse.ts`：支持 `GET /agent/runs/{run_id}/events?after=N` 的 Last-Event-ID 断线续传语义。
- `web-v2/lib/types.ts`：新增 Turn/Run/RunEvent/Clarification/Workspace 类型（对照 `bff/agent_api.py` 与 `models/agent.py`）。
- `web-v2/components/chat/ChatPanel.tsx` 重写状态机：发消息 → turn 状态（含阻塞/非阻塞澄清卡）→ run 进度事件流 → 从 workspace 取结果渲染卡片；支持取消 run。`ResearchProgress.tsx`、`StreamingText.tsx` 改为消费 run events。
- **保留旧契约的部分**：plan 创建（`POST /plans`）与首次全量规划流（`GET /plans/{id}/stream`）——v4 参照实现（web/index.html）亦如此；房间模式全部 `/rooms/*` 端点不变。
- 移除 web-v2 中对 `/plans/{id}/chat`、`/research-more` 的对话面板调用（ChatDecision 路径下线，design_itinerary 的 route_plan 渲染改由 v4 turn/workspace 结果承载；若 v4 侧未覆盖该意图，则在 respond/workspace 中补齐）。

## 阶段四：验证（三层全覆盖：自动化回归 + 活服务 API E2E + 真实浏览器交互带截图）

验收基线 = 两分支测试报告中的全部用例集合，缺一即视为回归。

### 4.1 自动化回归（离线，两分支测试全集）

- `uv run pytest` 全量：合并后应 ≥ codex 354 例 + gomate 新增（test_dd18_rooms 13 例、test_route_design、test_plan_bundle_endpoint 3 例）全绿；重点回归 test_v4_*（api/multiturn/prerequisites/reply_completeness/run_lifecycle/turn_state_machine）、test_turn_decision_refactor、test_candidate_lifecycle_refactor、test_open_semantic_agent、test_dd17、test_iterative_research、test_regression_v3_fixes。
- ruff 改动文件零告警；web-v2 `npm run build`（TS strict，15+ 路由）与 `npm run lint` 0 error 0 warning（对齐 DD-19 报告 §2 标准）。
- `uv run alembic upgrade head` 打通 0005 → 0006(agent) → 0007(rooms) 迁移链（Docker 隔离库 5433），并验证 downgrade 一级可回退。

### 4.2 活服务 API 级 E2E（真实 PG/Redis/LLM/Tavily，非 mock）

环境对齐 codex v4 报告：uvicorn :8000 + 独立 Worker 进程（`python -m wheretogo.agent.worker`）+ PG 5433 / Redis 6380 + 真实 LLM/搜索 key。

- 复跑 gomate 分支既有活服务脚本并适配合并后代码：`测试报告/e2e_dd18_rooms.py`（19 例房间全流程 R02–R19，含状态机非法跳转 409、转盘一次反悔、scope=local 深研、分享脱敏三重校验）、`live_e2e_dd19.py`（25 例对话+计划页链路，chat 部分改写为 v4 turns/events 契约）、`verify_weekend_activities.py`。
- 复跑探索性测试 W1 异常与边界 24 项（`explore_w1_edge_cases.py`）：不存在房间/邀请码/plan 的 404、错 member_token 403、非法投票权重/日期/负预算 422、非 RECOMMENDING 启推荐 409、空消息/缺字段 422、8000 字超长输入 422、SQL 注入样本无害、emoji+XSS 不 500；及 W2 对话组合 10 项（`explore_w2_chat_combos.py`，chat 部分改 v4 契约）：部分约束→追问→补全→规划流、中途改目的地、问价格、雨天方案、design_itinerary、无效订单回填不崩、ICS 格式、state 可读、对话历史落库。
- v4 API 层验证（对齐 codex 报告硬 KPI）：`/agent/metrics` 实测 `turn_silent_terminal_total=0`、`promised_without_run_total=0`、`hidden_clarification_total=0`；Idempotency-Key 幂等重发、run cancel、events `after=N` 续传逐项打点。
- 结果落盘：更新 `测试报告/e2e_dd18_results.json` 等结果文件。

### 4.3 真实浏览器交互验证（Chromium 真人操作 + 全程截图存档）

参照 codex `测试报告/v4_PRD场景全链路测试报告_2026-07-31.md` 的方式与用例编号，用 Browser subagent 驱动真实 Chromium 操作 web-v2（:3000），每个场景关键步骤截图，存 `测试报告/合并统一_E2E_截图_{日期}/`，并产出验证报告 md。

**A. 跨城对话规划（复刻 codex S1–S10 + MT 多轮用例，前端换成 web-v2 + v4 API）**
- S1 多人集合+上午景点+路途时长（三子任务逐项核对、非阻塞提示、候选卡证据角标）
- S2 完整路线+时段编排+地铁接驳（分段线路/换乘/耗时）
- S3 动线餐饮+忌口+人均预算（候选带语义匹配信号与来源）
- S4 禁止搜索本地重排（run_type=recompose、口头指定地点不静默丢弃——即 codex 已知缺陷 #3 的回归观察点）
- S5 证据诚实问答（门票价格：结构化+不编造）
- S6 跨城决策+门到门交通策略卡
- S7 订单回填 → 确认版时间线（含 kind=manual 回归）
- S8 天气变化重规划响应
- S9 多人匿名邀请聚合（party aggregate）
- S10 回复自洽性（内联完整行程无指针、天气正面回应）
- MT_01/MT_02 多轮累积（追加餐厅不丢锚点、变更重排保留显式点名项）
- 澄清死循环回归（codex 已修缺陷 #1：回答城市后不再追问）
- v4 交互专项：turn RUNNING → run 进度事件流实时渲染、断线刷新后 Last-Event-ID 续传、取消 run、阻塞/非阻塞澄清卡

**B. GoMate 房间模式（复刻 DD-18/DD-19 报告浏览器 10 步 + 探索性 W3 房间组合回退 9 项）**
- 主流程 10 步：建房→邀请码→第二人加入→MemberForm 全字段提交（出发地三选一/时间窗/兴趣标签/硬约束/预算/出行偏好）→便签墙共同窗/共同兴趣→转盘（服务端出结果+3s 动画落点一致+第 3 次 409）→确认主题→recommend SSE 深研进度+候选卡证据徽标→选定→集合信息（估算徽标）/成员路线/高德深链→VerticalTimeline 节点操作+七条快捷指令→「整体晚一小时」v1→v2（12:15→12:45）→撤销回 v1→分享页脱敏三重校验
- W3 回退与守卫 9 项：错误邀请码友好提示不白屏；路由守卫（COLLECTING 直访 /recommend、/plan → 重定向 /member）；无 token 访 member 页提示「去加入」；不存在房间提示；EXPIRED 房间只读页（所有操作禁用）；needs_confirmation 流（删核心活动→确认条→confirm:true 重发→undo 恢复）；投票路径 UI（计分/当前领先/按投票确认进推荐流）；推荐页 FilterBar/换一个重排；同机多成员 localStorage 身份覆盖行为符预期

**C. 对话链路前端渲染契约与既有缺陷回归（DD-19 联调 §3.1 + 探索性报告缺陷/W4 用例）**
- 对话链路渲染（换 v4 契约后逐项重验）：回复气泡→研究进度实时渲染→CityCard（城市/活动数/门到门/有效游玩/预算全 FactField 徽标）→TransportCard（高铁 vs 飞机）→探索版 PlanCard 按真实契约键渲染（activities/pending_checklist/time_windows/budget_range，非假设键——DD-19 缺陷 #5 回归）
- 问答意图劫持回归：「万兽之王演唱会门票多少钱」→ 不被当约束触发重跑，无数据时诚实回答不编造价格（探索性缺陷 #1 回归）
- 分享页回归：/plan 分享页已完成规划不显空态（bundle 兜底+真实契约键，探索性缺陷 #2）；房间分享页修改+撤销后脱敏复查（W4）
- chat-first 建 plan URL 无感更新且会话不丢（history.replaceState，DD-19 缺陷 #3）；刷新后服务端 plan/约束/历史不丢可继续对话（W4）
- `/plan/{id}` 刷新后 bundle 兜底端点渲染（DD-19 缺陷 #4）；/plan/999999999 优雅 404 提示（W4）
- 证据六态抽查：活动/时间/费用均带徽标，无裸值（W4）
- design_itinerary：点名双锚点「既要去A也要去B设计一条路线」→ RoutePlanCard 渲染（锚点时刻/警告/证据注记）
- BYO 订单回填表单 + ICS 订阅链接（VCALENDAR 格式）+ 分享页 OG/脱敏

**D. DD-19 §8 验收标准 18 项终审**
- 以 gomate 分支 `DD-19联调测试报告_2026-07-28.md` 的 18 项验收清单为基线逐项复核（含当时 2 项已知降级的复查），对话相关项按 v4 契约重新取证

### 4.4 验证产出物

- `测试报告/合并统一验证报告_{日期}.md`：逐用例 PASS/FAIL + 截图引用 + 发现缺陷与修复记录（格式对齐 codex v4 报告：结论摘要/环境表/逐场景结果/硬 KPI 实测）
- 截图目录 + 更新后的 E2E 结果 json + `/agent/metrics` 快照

## 关键决策记录

- research 两个增强均移植：`scope="local"` 是房间推荐硬依赖（不移植则 GoMate 模式崩溃）；`_upgrade_verified` 直接决定可信召回供给量，自包含低风险。
- 前端按用户要求全量迁移 v4 /agent API，不保留 ChatDecision 双轨。
- 迁移编号冲突以 codex 0006 优先（其为 AI 主线），rooms 顺延为 0007。

## 假设

- codex 分支的 `/plans`、`/plans/{id}/stream`、`/rooms/*` 等非对话端点契约在合并后保持稳定，前端无需改造这些路径。
- gomate 对 `intel/`、`domain/compose.py`、`domain/destination.py`、`seeds/` 的修改与 codex 无冲突（git 已确认非重叠文件），随合并自动并入。
