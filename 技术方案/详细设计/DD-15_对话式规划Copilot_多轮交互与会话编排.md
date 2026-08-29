# DD-15 对话式规划 Copilot（多轮交互与会话编排）· 详细设计

**详细设计系列 · v2 新增能力 · v1.1 · 2026 年 7 月**

> 本文定义 v2 的交互中枢：**对话式规划 Copilot**——用户在**聊天框**用自然语言表达需求，系统以**真多轮**对话从**模糊 → 具体 → 精细**逐步澄清与细化，并把每轮意图映射为对底层 LangGraph 状态机（DD-02）的驱动。它取代 v1.1 的"一次性约束收集 + 卡片流"。
>
> **上游依据**：v2 增补 D1；`01 竞品分析`（马蜂窝 AI 路书"主动提问/选择题补全"）；DD-02（状态机、`interrupt/resume`、可回退/重规划、SSE）；DD-16（会话/长期记忆）；DD-17（实时深搜触发与进度）；DD-07（结构化 schema 与多人聚合，被本模块编排）；DD-13（聊天 UI 渲染 + 证据六态）。
> **下游消费者**：DD-13 前端（渲染对话与卡片）、DD-02（被驱动）、DD-07/DD-17（被调度）。
> **一句话**：**聊天是入口，编排是内核——自然语言进来，确定性状态机干活，证据护栏兜底，模型只负责"听懂"和"解释"，不负责"编造"。**
>
> **v1.1 修订（2026-07，设计哲学修正）**：v1.0 的"小模型无状态抽取 + 规则优先"被否决——理解层**必须足够智能足够精准**：① NLU 抽取 prompt 注入对话上下文（`_context_block`：已记下约束摘要 + 近 6 轮对话原文 + 上一轮未决槽位提示），不再是只传当前一句；② 对话历史落库 `plans.conversation`（≤40 条），跨轮上下文有主存（checkpoint 之外的兜底）；③ 裸城市回答的上下文纠偏（缺 origins 且已有目的地时改判填 origins，详见 handle_turn）；④ `_summarize` 只对本轮真实变更的字段说"改"，历史回显用中性措辞；⑤ ask_info 问答改为"检索候选 + LLM 结合问题生成"（事实只允许引用行内数据，证据红线不破）；⑥ 规则/确定性兜底保留但**必须显式标注降级**（离线开发/CI 场景），不再是主路径。

---

## 1. 模块职责与边界

| 项 | 说明 |
|---|---|
| **职责** | ① 承接聊天框自然语言输入；② **多轮对话编排**：意图识别 + 粒度判定 + 澄清追问（可反复）；③ 把每轮映射为对 DD-02 的动作（首规划/字段修订/重规划/深搜/确认）；④ 组织流式回复（解释性文字 + 内嵌证据卡片 + 深搜进度）。 |
| **边界内** | 会话状态模型、意图分类与路由、澄清策略（多轮、任意粒度）、对 DD-02 的驱动映射、对 DD-16/DD-17 的调度、SSE 对话事件、降级。 |
| **边界外** | 具体规划算法（DD-08/09/11/12）；约束结构化 schema 与多人聚合（DD-07，被本模块调用）；记忆存取实现（DD-16）；深搜实现（DD-17）；事实定级（DD-03）；卡片视觉（DD-13）。 |
| **架构位置** | DD-02 的**对话外层/驱动层**：Copilot 解释每轮意图后，通过 `invoke/Command(resume)/update_state` 操作同一 `thread_id` 的图。 |

---

## 2. 设计目标与非目标

**目标**：① **自然语言 + 真多轮**，用户随时补充/更改/追问；② **任意粒度**：从"这周想出去玩"（模糊）到"周六上午看展、下午城市漫步、晚饭别太远"（精细）都能处理；③ **可澄清、可细化、可回退**；④ 会话开始**察觉历史偏好**（DD-16），少重复问。

**非目标**：❌ 不让 LLM 自由驱动流程（仍是确定性状态机编排，Copilot 只做"意图→动作"的翻译）；❌ 不让对话产出未经护栏的事实（解释性文字≠事实卡片，见 §9）；❌ v0.1 不做语音/多模态输入（延后）。

---

## 3. 会话状态与粒度模型

### 3.1 `TripPlanState` 新增会话字段（对齐 DD-02 §3）

```python
class ConversationTurn(TypedDict):
    role: Literal["user", "assistant"]
    content: str
    ts: str

# TripPlanState 增补
conversation: Annotated[list[ConversationTurn], operator.add]  # 多轮消息（reducer 追加）
intent: str | None            # 本轮意图（§4）
granularity: str | None       # 本轮粒度：coarse/medium/fine
pending_clarify: list[dict]   # 待澄清项（可为空=不追问）
memory_ctx: dict              # DD-16 注入的长期偏好/历史（会话开始加载）
```

### 3.2 粒度层级（决定驱动哪段图）

| 粒度 | 用户表达例 | 驱动 |
|---|---|---|
| **coarse 去哪** | "这周末想出去玩，预算 2000" | `parse`→`discover`（3 城市卡） |
| **medium 城市+交通** | "就去北京吧，周五晚出发" | 选定城 → `research`/`transport` |
| **fine 活动/餐饮/时间** | "周六想看展，晚饭别离酒店太远" | `revise` 相关字段 → `dining`/`timeline` 局部重排 |

> 同一轮可跨粒度；Copilot 判定后驱动对应节点，**未受影响部分不重算**（复用 DD-02 checkpoint/局部重规划）。

---

## 4. 意图分类与路由（会话编排内核）

每轮用户消息先经**意图分类**（DD-04 LLM，小模型），再**确定性路由**（不让模型直接执行）：

| intent | 含义 | 路由动作（对 DD-02） |
|---|---|---|
| `provide_constraints` | 提供/补充约束 | 更新 `constraints` → 首次则 `invoke`，否则 `update_state` 从 `parse` 续 |
| `clarify_answer` | 回答澄清问题 | 回灌澄清值 → 继续当前阶段 |
| `refine_field` | 改某字段（换城/调预算/改活动） | `update_state` + 从相关节点 `revise`（局部重算） |
| `deep_research` | 要最新/深挖 | 调 DD-17 `deep_research`（流式进度） |
| `confirm_booking` | 已买票回填 | 转 DD-10 回填流 → `Command(resume)` |
| `ask_info` | 问方案里的信息 | 只读 `state` 回答（带证据），不改图 |
| `design_itinerary` | 点名锚点要求排路线（"既要A也要B，帮我设计一条路线"） | **不驱动图**：锚点解析（库内活动，归一化匹配；未命中以 unknown 保留）+ 日路线求解（`domain/route_design.py`：白天/晚间分型、就近接驳、用餐占位，估算一律 estimated）→ 路线卡直出；消息自带约束先抽取落库（v1.1 增补） |
| `chitchat` | 闲聊/无关 | 礼貌回应 + 引导回规划，不驱动图 |

```python
async def handle_turn(plan_id: str, user_msg: str, emit) -> None:
    state = graph.get_state(cfg(plan_id))
    turn  = await classify_intent(user_msg, state, memory_ctx=state.values["memory_ctx"])  # LLM 小模型
    route = ROUTE_TABLE[turn.intent]
    # 缺关键约束 → 生成澄清项（多轮，可反复；命中记忆的槽位不问，DD-16）
    if clarifies := missing_after(turn, state):        # 复用 DD-07 missing_slots
        emit(assistant_clarify(clarifies)); return      # 等用户下一轮回答（真多轮）
    await route(plan_id, turn, emit)                     # 确定性驱动 DD-02（见 §5）
```

> **真多轮**：澄清不齐就发问、等答、再进；模型只产"问什么/怎么解释"，**执行永远是确定性路由 + 状态机**（守住 DD-02"LLM 不驱动流程"）。

---

## 5. 对 DD-02 的驱动映射（每轮 = 一次图操作）

```python
async def route_refine_field(plan_id, turn, emit):
    # 例："换成上海周边" / "预算降到 1500" / "周六下午想室内"
    patch = turn.field_patch                     # {"budget_band":{"max":1500}} 等
    graph.update_state(cfg(plan_id), patch, as_node=turn.reenter_node)  # 从相关节点回退
    async for ev in graph.astream(None, cfg(plan_id)):    # 局部重算，流式
        emit(to_sse(ev))                          # node_output/卡片事件 → DD-13
```

- 首次规划：`graph.astream(inputs, cfg)` 从 `parse` 跑到 `await_booking`（探索版）。
- 回填后：`Command(resume=bookings)` 续跑到 `compose`（确认版）。
- 细化/改动：`update_state(as_node=…)` + 续跑，**只重算受影响子图**（DD-02 §7/§8）。
- 深搜：`deep_research`（DD-17）流式进度 → 结果入库 → 触发相关节点复算。

> Copilot **不自己算方案**，只把"人话"翻成"对状态机的操作"，再把状态机产物翻成"人话 + 卡片"。

---

## 6. 与记忆（DD-16）接线

- **会话开始**：`load_memory(user_id)` → `memory_ctx`（长期偏好/历史/去过的城）注入 `state` 与意图分类/澄清 prompt（命中的槽位**不再追问**）；
- **会话中**：多轮消息即会话工作记忆（`conversation` + checkpoint）；
- **会话结束/关键节点**：`extract_and_write_memory(conversation, plan)` → DD-16 写 Mem0（带覆盖语义）。

---

## 7. 流式对话事件（SSE，供 DD-13）

```jsonc
{ "event":"assistant_delta", "text":"好的，帮你看这个周末…" }        // 解释性文字流
{ "event":"clarify", "questions":[{"q":"几个人出行？","options":["1","2","3+"]}] }  // 多轮追问
{ "event":"node_output", "node":"discover", "data":{ /* 城市卡，字段带 evidence */ } }
{ "event":"research_progress", "phase":"verify", "message":"正在核实官方来源…" }   // DD-17
{ "event":"interrupt", "node":"await_booking", "data":{ /* 探索版+预填+起售 */ } }
{ "event":"done", "node":"compose", "data":{ "bundle":{...} } }
```

> 前端（DD-13）：`assistant_delta`/`clarify` 渲染为对话气泡；`node_output`/`interrupt`/`done` 渲染为**内嵌卡片**，事实字段一律经 `FactField` 六态渲染。

---

## 8. 与 DD-07 的关系（不重复造轮子）

- DD-07 的 `parse_single/parse_multi`（结构化 schema）、`missing_slots`（缺口检测）、`aggregate_party`（多人聚合）**保留并被本模块调度**；
- 变化：v1.1 的"BFF 单轮补全"升级为"**Copilot 多轮澄清**"——`missing_slots` 的产出由 Copilot 分轮、按对话节奏发问（可反复），而非一次性 ≤4 题。

---

## 9. 证据边界（对话不破坏"证据优先"）

- **解释性文字**（"这个周末北京有几个不错的展"）可由 LLM 生成，属"对话语气"，**不是事实断言**；
- **一切事实**（活动时间/价格/门到门/营业时间）**只出现在卡片里，且经 `Fact/Evidence` 六态渲染**（DD-03/DD-13）；
- Copilot 的系统提示强制："事实一律引用卡片字段，不在正文里编造数字；不知道就说不知道并触发深搜或给官方入口"；
- 交通票价/余票：对话里也**不生成**，引导去官方（DD-03 闸三不变）。

---

## 10. 降级

| 失效 | 降级 |
|---|---|
| 意图分类 LLM 挂 | 回退**结构化向导**（DD-07 老路径：表单/选择题一次性收集），流程不断 |
| 深搜超时（DD-17） | 对话提示"已返回已核实部分 + 官方源清单"，继续规划 |
| 澄清反复不收敛 | N 轮后用缺省 + `warning` 先出探索版（DD-02 §10），让用户在结果上改 |

---

## 11. 效果与验收标准（DoD）

1. **真多轮**：模糊输入（"想出去玩"）能经多轮澄清收敛到探索版；中途改约束（"预算降一半"）局部重算、不推翻已确认项。
2. **任意粒度**：coarse/medium/fine 三类输入分别正确驱动 discover/transport/局部 revise（路由准确率达标）。
3. **记忆生效**：老用户会话，命中偏好的槽位不再追问（对照冷启动会话）。
4. **实时**：说"查最新" → 触发 DD-17 并流式展示进度与结果。
5. **不编造**：对话正文无事实数字硬编造；事实全在六态卡片里（人工+CI 抽检）。
6. **降级**：mock 意图 LLM 故障 → 回退结构化向导仍可完成规划。

---

## 12. 开发任务拆解 + 风险

**任务**：① 会话状态字段 + SSE 对话事件（1d）；② 意图分类（LLM）+ 确定性路由表（1.5d）；③ 多轮澄清编排（复用 DD-07 missing_slots）（1d）；④ 驱动映射（invoke/resume/update_state 局部重算）（1.5d）；⑤ 记忆注入/回写接线（DD-16）（1d）；⑥ 深搜触发/进度对接（DD-17）（1d）；⑦ 降级与验收（1d）。

| 风险 | 缓解 |
|---|---|
| 对话"跑飞"/模型自作主张执行 | 意图→**确定性路由**，模型不直接操作图；关键动作需状态机确认 |
| 多轮澄清打扰用户 | 命中记忆不问；澄清合并、可跳过；N 轮兜底出探索版 |
| 正文编造事实 | 事实只走卡片 + 六态；系统提示 + CI 抽检 |
| 局部重算引发状态错乱 | 用 `update_state(as_node)` 精确回退 + `lock:plan` 串行（DD-02 §9） |

---

> 本模块把"卡片流工具"升级为"对话式规划伙伴"，但内核仍是 DD-02 确定性状态机 + DD-03 护栏——**对话让它好用，护栏让它可信**。
