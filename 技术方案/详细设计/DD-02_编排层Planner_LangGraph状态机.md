# DD-02 编排层 Planner（LangGraph 状态机）· 详细设计

**详细设计系列 · 编排契约文档 · v1.0 · 2026 年 7 月**

> 本文定义全系统的**编排骨架**：`TripPlanState` 状态、图拓扑、节点编排契约、两段式 `interrupt/resume`、可回退、重规划、对 BFF 的接口。各领域模块（DD-06 ~ DD-13）以**节点**形式挂到本图上，其"输入/输出 state 字段"以本文 §5 契约表为准。
>
> **上游依据**：v1 §5（状态机）、v1.1 增补 E（reducer/并行/依赖边）、DD-01（状态持久化、`constraints` schema）。
> **下游消费者**：DD-06 ~ DD-13（作为节点）、BFF/前端（驱动方）。
> **强约束**：LLM 只在节点内做子调用，**不驱动流程**；规划流对 `activities` **只读**（读写解耦）。

---

## 1. 模块职责与边界

| 项 | 说明 |
|---|---|
| **职责** | 用 LangGraph 显式状态机编排两段式规划：约束 → 目的地发现 → 活动研究 → 交通策略 →〔中断等回填〕→ 住宿 → 接驳 → 餐饮 → 时间线 → 校验 → 组装；提供**跨天中断/恢复、可回退、重规划**。 |
| **边界内** | 状态 schema、图拓扑与边、节点编排契约、`interrupt/resume`、checkpoint、replan、错误/降级、对 BFF 的 API 与 SSE 事件。 |
| **边界外** | 各节点内部算法（在 DD-06~DD-13）。本文只规定节点的**输入/输出契约与调用关系**。 |
| **架构位置** | v1 §4.1 分层图"编排层"，v1 §5。 |

---

## 2. 设计目标与非目标

**目标**：① 每步状态可见、可回退；② "等回填"是一等公民（跨天恢复、不重算前序）；③ 确定性控制（门到门/排程/预算都是确定性节点，LLM 仅子调用）；④ 节点解耦（领域逻辑可独立开发、独立单测）。

**非目标**：❌ 对话式多智能体自由协作；❌ 让 LLM 决定流程走向；❌ 在规划流里做实时抓取（违背读写解耦）。

---

## 3. 状态定义 `TripPlanState`（权威，含 reducer）

```python
from typing import TypedDict, Literal, Annotated
import operator

class TripPlanState(TypedDict):
    # —— 标识与阶段 ——
    plan_id: str
    stage: Literal["explore", "await_booking", "confirm"]      # 与 DD-01 plan_stage 一致
    # —— 输入（DD-07 写） ——
    constraints: dict            # DD-01 §8.1 constraints schema（聚合后的匿名约束）
    party: list[dict]            # 各同行人脱敏约束（仅聚合展示）
    # —— 探索阶段产物 ——
    candidate_cities: Annotated[list[dict], operator.add]  # DD-08 写；并行扇出追加
    activities: Annotated[list[dict], operator.add]        # DD-05/DD-06 只读检索结果追加
    transport_options: dict      # DD-09 写：门到门比较 + 策略卡 + 深链 + 起售提醒
    # —— 回填（DD-10 在中断期写入，resume 注入） ——
    bookings: list[dict]         # 已确认的车次/航班/酒店（confirmed_by_user）
    # —— 确认阶段产物 ——
    hotel_area: dict             # DD-11 写
    local_routes: Annotated[list[dict], operator.add]      # DD-11 写
    dining: list[dict]           # DD-11 写
    timeline: list[dict]         # DD-12 写
    validation: dict             # DD-12 写：硬约束校验结果
    bundle: dict                 # DD-13 写：explore/confirm 版 Trip Bundle
    # —— 横切 ——
    warnings: Annotated[list[str], operator.add]           # 多节点追加
    errors: Annotated[list[dict], operator.add]            # 节点级错误（降级用）
    replan_reason: str | None    # 重规划触发原因（weather/info_change/manual）
```

> **reducer 铁律**（v1.1 E①）：所有可能被并行节点写的**列表**字段用 `Annotated[list, operator.add]`，否则并行汇聚丢更新。`dict`/标量字段默认 `LastValue`（覆盖写），由单一 owner 节点写。

---

## 4. 图拓扑

```text
         START
           │
        [parse]  ConstraintParser (DD-07, LLM 子调用)
           │
        [discover]  DestinationDiscovery (DD-08) ──并行扇出──┐
           │         └ 每候选城市并行: 门到门粗估 + 活动检索(DD-05) │
           │◄────────────────── 屏障节点(reducer 合并) ◄────────┘
        [research]  ActivityResearch (DD-05 检索 + 必要时 DD-06 补搜)
           │
        [transport]  TransportStrategy (DD-09, 确定性门到门)
           │
   ╔═══════▼═══════ 探索版 Trip Bundle 输出（DD-13 compose_explore）═══════╗
   ║           [await_booking]  interrupt() —— 持久化，等用户回填              ║
   ╚═══════════════════════════════╤══════════════════════════════════════╝
           │ Command(resume=confirmed_bookings) 恢复（跨天）
        [hotel]  HotelAreaPlanning (DD-11)
           │
        [mobility]  LocalMobility (DD-11 + DD-04 map)
           │
        [dining]  DiningPlanning (DD-11 + DD-05 重排)
           │
        [timeline]  TimelineSolver (DD-12)
           │
        [validate]  FeasibilityValidator (DD-12)
           │ 条件边：ok → compose；hard_conflict → 回 timeline 或 transport 重排
        [compose]  PlanComposer (DD-13, 确认版 bundle)
           │
          END
     （重规划入口：replan 触发 → 依 reason 从 discover / timeline / dining 重入）
```

**图构建代码**（v1.1 E②③：真连依赖边 + 并行 + 屏障）：

```python
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.postgres import PostgresSaver

def build_graph(checkpointer):
    g = StateGraph(TripPlanState)
    for name, fn in [("parse", constraint_parser), ("discover", destination_discovery),
                     ("research", activity_research), ("transport", transport_strategy),
                     ("await_booking", await_booking_node), ("hotel", hotel_area_planning),
                     ("mobility", local_mobility), ("dining", dining_planning),
                     ("timeline", timeline_solver), ("validate", feasibility_validator),
                     ("compose", plan_composer)]:
        g.add_node(name, fn)
    g.add_edge(START, "parse")
    for a, b in [("parse","discover"),("discover","research"),("research","transport"),
                 ("transport","await_booking"),("await_booking","hotel"),
                 ("hotel","mobility"),("mobility","dining"),("dining","timeline"),
                 ("timeline","validate")]:
        g.add_edge(a, b)
    g.add_conditional_edges("validate", route_after_validate,
                            {"ok": "compose", "reflow": "timeline", "retransport": "transport"})
    g.add_edge("compose", END)
    return g.compile(checkpointer=checkpointer)

def route_after_validate(state) -> str:
    v = state["validation"]
    if v.get("ok"): return "ok"
    if "RETURN_TIGHT" in v.get("issues", []): return "retransport"  # 返程太赶→重挑交通
    return "reflow"                                                  # 其它硬冲突→重排时间线
```

---

## 5. 节点编排契约表（★ 全系列 interlocking 的核心）

> 每个节点是一份"纯函数"约定：给定 **读入字段**，产出 **写出字段**，中途允许调用列出的**领域模块/Provider**；标注是否受 **Provenance Guard**（DD-03）约束。开发者据此可**独立实现与单测每个节点**（mock 掉读入字段即可）。

| 节点 | 读入 state 字段 | 写出 state 字段 | 调用（模块/Provider） | 受 Guard | 降级行为 |
|---|---|---|---|---|---|
| `parse` | constraints(原始), party | constraints(结构化), warnings | DD-07；LLM(小/中) | 否（不产事实） | 追问不全→带缺省+warning 继续 |
| `discover` | constraints | candidate_cities | DD-08；DD-05 检索；DD-04 map/weather | 是（城市卡字段带 evidence） | 跨城不成立→近郊/同城候选 |
| `research` | constraints, candidate_cities | activities | DD-05 检索；必要时 DD-06 补搜 | 是（活动带 verification_status） | 检索空→关键词+官方源清单 |
| `transport` | constraints, candidate_cities | transport_options | DD-09（门到门/12306/航班） | 是（禁编票价/余票） | 无航班数据→机场对比+时段建议 |
| `await_booking` | transport_options, candidate_cities | bundle(explore), bookings(resume 注入), stage | DD-13 compose_explore；`interrupt()` | 否 | — |
| `hotel` | bookings, candidate_cities(选定城) | hotel_area | DD-11；DD-04 map | 是（区域卡带 evidence） | 无 POI→区域+筛选条件 |
| `mobility` | bookings, hotel_area, activities(选定) | local_routes | DD-11；DD-04 map(路线/距离矩阵) | 是（source=amap/rule） | 额度受限→缓存+地图链接 |
| `dining` | timeline(草稿), hotel_area, constraints | dining | DD-11；DD-05 重排；DD-04 POI | 是（营业时间带来源） | POI 不足→区域+稳妥备选 |
| `timeline` | bookings, activities(选定), dining, local_routes | timeline | DD-12（贪心排点） | 否（组合已带证据字段） | — |
| `validate` | timeline, constraints | validation, warnings | DD-12（硬约束校验） | 否 | 硬冲突→条件边回退重排 |
| `compose` | 全部产物 | bundle(confirm) | DD-13；**出稿前跑 Provenance Guard 断言** | **是（最终闸）** | — |

> **读写解耦铁律**：任何节点对 `activities` 只读（写在 DD-06 情报流水线）；`bookings` 只在 `await_booking` 经 resume 注入（写来自 DD-10 的用户确认）。

---

## 6. 两段式 `interrupt / resume`（定义性能力）

### 6.1 中断节点

```python
from langgraph.types import interrupt

def await_booking_node(state: TripPlanState):
    explore_bundle = compose_explore_bundle(state)      # 调 DD-13：先产出可分享探索版
    user_bookings = interrupt({                          # 持久化 → 交回控制权给用户
        "type": "await_booking",
        "explore_bundle": explore_bundle,
        "prefill": build_prefill_hints(state["transport_options"]),  # DD-09 预填清单
        "presale_reminders": state["transport_options"].get("presale"),
    })
    return {"bookings": user_bookings, "stage": "confirm"}
```

### 6.2 中断 payload / resume 契约

```jsonc
// interrupt 抛给 BFF 的 payload
{ "type": "await_booking",
  "explore_bundle": { /* DD-13 探索版 bundle，每字段带 evidence */ },
  "prefill": { "rail": {...}, "flight": {...} },     // 供用户去官方平台一键复制
  "presale_reminders": [ {"train_window":"周六早","open_at":"..."} ] }

// BFF 恢复时传回（来自 DD-10 用户逐字段确认后的结果）
{ "resume": [ {"kind":"train","extracted":{...},"confirmed":true,"evidence":{...}}, ... ] }
```

```python
# BFF 恢复（几小时/几天后），从 Postgres 检查点续跑，不重算 parse~transport
graph.invoke(Command(resume=confirmed_bookings),
             config={"configurable": {"thread_id": f"plan:{plan_id}"}})
```

### 6.3 时序

```text
用户提交约束 → BFF invoke(graph) → parse..transport 执行 → await_booking:
  产出探索版 → interrupt 持久化 → BFF 收到中断 → 前端展示探索版+预填清单+起售提醒
[用户离开去 12306/航司买票……几小时/几天]
用户回填(DD-10 抽取+确认) → BFF resume(Command) → 从 checkpoint 恢复 →
  hotel..compose 执行 → 产出确认版 bundle → END
```

---

## 7. 可回退 / 时间旅行

- **能力**：`graph.get_state_history(config)` 取 checkpoint 历史；`graph.update_state(config, values, as_node=...)` 回到任一节点改状态再续跑；可 fork 新分支。
- **产品语义**：用户"改约束/否决某候选城/换交通方式" → 回到 `discover` 或 `transport` 重算下游；**已确认 `bookings` 不动**。
- **实现**：回退操作由 BFF 暴露为"编辑并重算"动作，内部映射到 `update_state` + 从该节点续跑。

---

## 8. 重规划触发器（Replan）

| 触发 | 来源 | 重入节点 | 说明 |
|---|---|---|---|
| 天气变化 | DD-13 天气检查（行前 72h/24h） | `dining`/`timeline` | 室外降权、增室内备选，重排时间线 |
| 信息变更 | 用户改约束/回填修正 | `discover` 或 `transport` | 依变更范围决定重入点 |
| 手动刷新 | 用户主动 | 相应节点 | 只在有意义时刻打扰 |

```python
def trigger_replan(plan_id, reason, from_node):
    cfg = {"configurable": {"thread_id": f"plan:{plan_id}"}}
    graph.update_state(cfg, {"replan_reason": reason}, as_node=from_node)
    graph.invoke(None, cfg)     # 从 from_node 续跑，只重算受影响子图；bookings 保留
```

---

## 9. 持久化与恢复、并发一致性

- **Checkpointer**：`PostgresSaver`（DD-01 §9.3），`thread_id = plan:{id}`，一个计划一条 checkpoint 线。
- **恢复不重算前序**：LangGraph 从最近 checkpoint 续跑（官方保证）。
- **并发/一致性（BSP）**：并行扇出（`discover` 多城市）汇聚到下游前**显式加屏障节点**，避免"节点被调度两次"（v1.1 E 隐坑）；同一 `plan_id` 的 resume/replan 用 `lock:plan:{id}`（DD-01 §9.1）串行，避免并发写 checkpoint。
- **幂等**：节点应可重入（相同输入→相同输出），便于重算与重试。

---

## 10. 错误处理与降级

- **节点内失败**：捕获后写 `errors` + `warnings`，产出**部分结果 + `unknown` 标注**，不整链失败（对应 v1 §10 韧性"任何单点失效仍拿到有用输出"）。
- **Provider 熔断**：由 DD-04 `ResilientProvider` 处理，节点收到的是**降级结果**（备用源/规则兜底/明确 unknown），节点照常产出并标注。
- **AI 异常**：LLM 不可用时，`parse` 用规则模板解析、`discover`/`compose` 用城市档案 + 确定性服务生成**简化计划**（不依赖 LLM）。
- **校验回退**：`validate` 硬冲突 → 条件边回 `timeline`/`transport`，最多回退 N 次后产出"带风险提示"的次优方案。

---

## 11. 对 BFF 的接口契约

| 方法 | 路径 | 说明 | 返回 |
|---|---|---|---|
| POST | `/plans` | 启动规划（body: 约束/邀请） | `plan_id` + SSE 流 |
| GET | `/plans/{id}/stream` | SSE：逐节点产物流式推送 | event 流（见下） |
| POST | `/plans/{id}/resume` | 回填确认后恢复（body: bookings） | 202 + 继续 SSE |
| GET | `/plans/{id}/state` | 查询当前 stage 与产物 | TripPlanState 快照（脱敏） |
| POST | `/plans/{id}/replan` | 触发重规划（body: reason） | 202 + 继续 SSE |
| POST | `/plans/{id}/revise` | 回退并重算（body: 改动+from_node） | 202 |

**SSE 事件 schema**（对齐 AG-UI 思路，前端只需按类型渲染卡片）：
```jsonc
{ "event": "node_output", "node": "discover",
  "data": { "candidate_cities": [ /* 每字段带 evidence，前端据此渲染六态 */ ] } }
{ "event": "interrupt", "node": "await_booking", "data": { /* payload §6.2 */ } }
{ "event": "done", "node": "compose", "data": { "bundle": {...} } }
{ "event": "error", "node": "transport", "data": { "message": "...", "degraded": true } }
```

---

## 12. 可观测（KPI 埋点）

- 每节点一个 OTel span（LangSmith 或 Langfuse，DD-04 §可观测）；记录 token、耗时、Provider 调用、降级次数。
- **硬 KPI 埋点**：`compose` 出稿前的 Provenance Guard 断言（DD-03）计数——"未确认字段被误标为已确认 = 0"，任何一次违规即报警。
- 埋点：探索版产出时长、回填完成率、确认版产出时长、重规划次数。

---

## 13. 效果与验收标准（DoD）

**功能验收**：
1. 端到端跑通两段式：约束 → 探索版 → 中断 → 回填 → 确认版。
2. **跨天恢复**：中断后重启进程/隔日 resume，不重算 `parse~transport`（用 checkpoint 计数验证）。
3. **可回退**：`revise` 改约束回到 `discover` 重算，已确认 bookings 保留。
4. **重规划**：天气触发从 `timeline` 重入，仅受影响槽位变化。
5. **降级**：mock 掉 LLM / 某 Provider，仍产出带 `unknown` 标注的可用结果。

**测试用例**（pytest + LangGraph 内存 checkpointer 起步，再切 Postgres）：
- 正常流、跨天恢复、并行扇出无丢更新、条件边回退、Provider 熔断降级、Guard 断言拦截。

---

## 14. 开发任务拆解

1. `TripPlanState` + reducer + 图骨架（真连边）（1d）
2. `await_booking` interrupt + resume 打通（含 Postgres checkpoint）（1.5d）
3. 节点桩（stub）：先返回 mock，联通图与 SSE（1d）
4. 条件边/回退/重规划（1d）
5. BFF 接口 + SSE 事件（配合前端）（1.5d）
6. 可观测接线 + Guard 断言埋点（1d）
7. 验收用例（1d）

## 15. 风险与缓解

| 风险 | 缓解 |
|---|---|
| BSP 超步/reducer 隐坑（节点被调度两次） | 并行汇聚显式加屏障节点；code review 检查图拓扑 |
| checkpoint 状态膨胀 | TTL/归档（DD-01 §11.3）；state 里不塞大 payload（大对象落表存引用） |
| LangGraph 版本演进快 | 锁定 1.x minor + 回归测试；节点领域逻辑不绑定框架（可迁移） |
| resume 并发写 | `lock:plan` 串行 |

---

> 本文的 §5 节点契约表是 DD-06~DD-13 的**对接基准**。任何节点变更读写字段，须回改本表并通知相关模块。

---

## 16. v2 增补：对话式编排 + 深研节点 + 记忆注入（对齐 DD-15/16/17）

**① `TripPlanState` 增补字段**（权威定义见 DD-15 §3.1）：`conversation`(reducer 追加)、`intent`、`granularity`、`pending_clarify`、`memory_ctx`、`research`。

**② 新增节点 `deep_research`**（DD-17）：

| 节点 | 读入 | 写出 | 调用 | 受 Guard | 降级 |
|---|---|---|---|---|---|
| `deep_research` | constraints, candidate_cities, research | activities(经 DD-06 实时入库), research | DD-17（**有界迭代环**：Supervisor+并行子研究+反思补缺，R_max/时间预算/开关走 `.env`）；DD-04 深搜/LLM | 是（实时结果同过 DD-03 定级） | 超时 partial + 官方源清单 |

**③ 图拓扑增补（v2 强制全量）**：`research` **每轮强制进 `deep_research`**（同步流式，不因库覆盖跳过）→ 实时入库后回 `research` 复检；库结果先垫场秒级展示，深搜结果流式融合；超时 `partial` 兜底。

**④ 对话驱动（DD-15 为外层）**：Copilot 通过 `invoke` / `Command(resume)` / `update_state(as_node=…)` 操作同一 `thread_id` 的图；**每轮用户消息 = 一次图操作**；**多轮澄清 = 多次 `interrupt`**（与两段式回填的 interrupt 机制一致）。

**⑤ 记忆注入（DD-16）**：会话开始 `load_memory(user_id)` → `memory_ctx` 注入 `parse`/意图分类/澄清（命中偏好不再追问）；会话结束 `extract_and_write` 回写。

> **不变的铁律**：LLM 仍不驱动流程（DD-15 只做“意图→确定性路由”）；深研结果仍过 DD-03 护栏；交通票价/余票禁编（闸三）不动。
