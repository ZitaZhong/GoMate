# Agent 框架调研洞察与技术选型

**版本 v1.0 · 面向「周末去哪儿」的独立技术调研 · 2026 年 7 月**

> 本文是一份**独立的 Agent 框架横向调研**，服务于两个目的：
> 1. 深入剖析 2026 年业界主流 Agent 框架的**架构、设计哲学、代码实现、优劣、适用场景、效果与扩展性**；
> 2. 在此基础上，回到 `周末去哪儿_产品方案_对外版.md` 与 `周末去哪儿_技术架构与实现方案_v1.md` 的真实约束，给出**有据可依的技术选型**。
>
> 结论会与既有技术方案（已选 LangGraph）**交叉验证**：既有方案是"先选型后论证"，本文是"先把整个赛道摊开、再回推选型"，用以证伪或加固原决策。
>
> ⚠️ **v2 补充（避免误解）**：本文所排除的"对话式多智能体自由协作 / 模型驱动自治(Deep Agents)"**仍然成立**。v2 新增的**实时深度研究**（DD-17）采用 LangChain Open Deep Research 的 **Supervisor + 并行子研究**——这是 **LangGraph 内的有界多智能体**（仅用于并行研究子任务、受 R_max/时间预算约束、结果过证据护栏），**不是**被排除的"自由对话式多智能体"；对话式 Copilot（DD-15）是"意图→确定性路由"，也**非**模型自治。二者与本文选型**不矛盾**。

---

## 目录

1. [结论先行（TL;DR）](#1-结论先行tldr)
2. [调研方法与评估维度](#2-调研方法与评估维度)
3. [2026 Agent 框架全景与分类地图](#3-2026-agent-框架全景与分类地图)
4. [主流框架深度剖析](#4-主流框架深度剖析)
   - 4.1 [LangGraph](#41-langgraph图状态机bsp-运行时)
   - 4.2 [Microsoft Agent Framework（AutoGen + Semantic Kernel）](#42-microsoft-agent-frameworkautogen--semantic-kernel-的合流)
   - 4.3 [OpenAI Agents SDK](#43-openai-agents-sdk极简原语)
   - 4.4 [CrewAI](#44-crewai角色团队--flows)
   - 4.5 [LlamaIndex Workflows](#45-llamaindex-workflows事件驱动)
   - 4.6 [Google ADK](#46-google-adk电池全含gcp-原生)
   - 4.7 [PydanticAI](#47-pydanticai类型安全优先)
   - 4.8 [轻量与专用：Agno / smolagents / Strands / Mastra](#48-轻量与专用agno--smolagents--strands--mastra)
   - 4.9 [Harness 类：Claude Agent SDK / Deep Agents](#49-harness-类claude-agent-sdk--deep-agents)
   - 4.10 [低代码平台：Dify / Coze](#410-低代码平台dify--coze)
5. [横向对比矩阵](#5-横向对比矩阵)
6. [五种架构范式的本质区别](#6-五种架构范式的本质区别)
7. [互操作协议：MCP / A2A / AG-UI](#7-互操作协议mcp--a2a--ag-ui)
8. [回到本 PRD：需求画像 → 框架能力映射](#8-回到本-prd需求画像--框架能力映射)
9. [技术选型建议](#9-技术选型建议)
10. [选型落地要点与风险](#10-选型落地要点与风险)
11. [附录：速查表](#11-附录速查表)

---

## 1. 结论先行（TL;DR）

**一句话结论：既有方案选择 LangGraph 作为核心编排引擎，经本次独立横评，结论成立且是当前赛道里最优解——因为本产品的定义性需求（跨天可中断、可持久化、可回退、证据优先的两段式状态机）恰好命中 LangGraph 的架构原生能力，而其它框架都需要"外挂"才能补齐这一能力。**

分层选型（而非单一框架打天下）：

| 层 | 选型 | 一句话理由 |
|---|---|---|
| **核心编排（主选）** | **LangGraph** | 唯一把「持久化 checkpoint + 原生 `interrupt` 跨天恢复 + 时间旅行回退 + 确定性节点控制」做成**框架原生**的方案，直接对应两段式规划 |
| **结构化抽取/护栏（配套）** | **PydanticAI 或 Pydantic + Function Calling** | 类型安全的结构化输出天然承载 `Fact/Evidence` 模型与 Provenance Guard；作为 LangGraph 节点内的 LLM 子调用 |
| **工具/数据连接（标准）** | **MCP（Model Context Protocol）** | 把地图/天气/航班/搜索封装成 MCP Server，对齐 PRD v0.3「开放数据源插件机制」，浏览器扩展亦可复用 |
| **文档解析（可选组件）** | **LlamaIndex / LlamaParse 的解析件** | 只借其**文档解析**能力用于活动情报流水线，**不**作为编排器 |
| **可观测（配套）** | **LangSmith 或 Langfuse（OTel）** | 框架无关，给证据 KPI（未确认字段误展为已确认=0）提供埋点与回放 |

**明确排除**：对话式多智能体（AutoGen/CrewAI 的角色扮演）、模型驱动自治（Strands/smolagents）、低代码黑盒（Dify/Coze）、云绑定运行时（Google ADK/GCP）。理由见第 8、9 节——它们要么与"证据优先的确定性控制"相冲突，要么与技术栈/中立性约束相冲突。

---

## 2. 调研方法与评估维度

**调研来源**：各框架官方文档与 GitHub、2026 年多份横向评测（含 LangChain 官方《The best AI agent frameworks in 2026》、生产实测榜单、多家深度剖析博客），并交叉核对社区（Reddit / HN / GitHub Issues）暴露的真实生产摩擦点，而非只看 README 宣称。

**评估维度**（贯穿每个框架）：

1. **架构（Architecture）**：执行模型是什么（图/状态机、对话、角色团队、事件、模型驱动循环、harness）。
2. **设计哲学（Design）**：控制权在开发者还是模型手里；抽象是"暴露内部"还是"隐藏内部"。
3. **代码实现（Implementation）**：核心原语与真实代码形态。
4. **优劣（Pros/Cons）**：结构性长处与结构性短板。
5. **适用场景（Fit）**：什么问题它最擅长。
6. **效果（Effectiveness）**：生产可靠性、可观测、token 成本的实测信号。
7. **扩展性（Extensibility）**：加工具、换模型、接协议、水平扩容的成本。

**一条贯穿全文的判据**：*好的 Agent 框架，其抽象应当在加速正确决策的同时"暴露足够的内部状态以便推理与排障"；隐藏了失败模式的抽象，省下的搭建时间会在排障时加倍还回去。* 这条判据对"证据优先、诚实为硬 KPI"的本产品尤其致命。

---

## 3. 2026 Agent 框架全景与分类地图

到 2026 年，Agent 框架已从"百花齐放"沉淀为**六大范式**。按"控制权归属"从左到右排列（开发者显式控制 → 模型自主驾驶）：

```text
显式控制 ───────────────────────────────────────────────► 模型自治
│                │                │              │            │
图/状态机编排     事件驱动          角色/团队       对话式        模型驱动循环 / Harness
LangGraph        LlamaIndex        CrewAI         (AutoGen)     Strands / smolagents
MS Agent Fwk     Workflows         Agno(Team)     →合流入MAF     Claude Agent SDK
(graph workflow)                                                Deep Agents
                 └────────── OpenAI Agents SDK（极简原语，居中偏右）──────────┘

低代码/可视化（黑盒，另一维度）：Dify · Coze · n8n
互操作协议（横向标准）：MCP（Agent↔工具） · A2A（Agent↔Agent） · AG-UI（Agent↔前端）
```

**2026 年的四个关键事实**（决定了赛道格局）：

- **LangGraph 1.0 已 GA**（2025-10），成为"可持久化状态机 + 人在环"的事实标准运行时；被 Uber、LinkedIn、摩根大通等采用。
- **微软把 AutoGen 与 Semantic Kernel 合并为 Microsoft Agent Framework**（2025-10 宣布，Python+.NET 于 2026-04-03 双双 1.0 GA）；AutoGen/SK 转入维护期。这终结了微软系"两个框架并存"的混乱。
- **OpenAI Agents SDK**（2025-03 发布，2026-04 加入原生 sandbox）成为"极简原语"代表；**Claude Agent SDK** 成为"编码/自治 harness"代表。
- **MCP 与 A2A 成为跨框架标准**：几乎所有主流框架（LangGraph、MAF、OpenAI SDK、CrewAI、ADK、Mastra…）都原生支持 MCP；A2A 由 Google 提出并交由 Linux Foundation 托管。**框架竞争的重心正从"能不能编排"转向"可观测性、持久化、协议互通"。**

GitHub 热度（2026 年中量级，供参考，非选型依据）：LangChain ~134k、CrewAI ~49k、OpenAI Agents SDK ~22k、Mastra ~23k、Google ADK ~19k、MS Agent Framework ~9.6k（新）。

---

## 4. 主流框架深度剖析

> 每个框架统一按"定位 → 架构 → 设计 → 代码 → 优劣 → 场景 → 扩展性/效果"展开。代码片段为**说明性**，用以呈现编程模型形态。

### 4.1 LangGraph（图状态机／BSP 运行时）

**定位**：LangChain 团队出品的**低层编排运行时**（与 LangChain 本体分离，可独立使用）。官方口径已明确"做 Agent 用 LangGraph，而非 LangChain"。

**架构——这是全篇最需要讲透的一个，因为它决定了本产品的可行性。**

LangGraph 把 Agent 系统建模为**有向图 + 显式共享状态**，其运行时不是普通的 DAG 调度，而是移植了 Google Pregel 的 **BSP（Bulk Synchronous Parallel，整体同步并行）** 模型：

- **状态（State）是资产，转移是工作**：你用 `TypedDict`/Pydantic 定义在图中流动的状态 schema。
- **Channel + Reducer**：状态的每个字段是一个 channel，节点**不直接改共享内存，而是向 channel 发布更新**；channel 类型决定合并语义：
  - `LastValue`（默认）：覆盖写（如"最新查询"）；
  - `BinaryOperatorAggregate`：用二元算子（如 `operator.add`、`add_messages`）在**屏障处**合并——这是并行安全更新的基石，多个节点同一超步写同一 channel 也不会丢更新、不产生竞态；
  - `Topic`：pub/sub 事件流。
- **超步（Superstep）与屏障（Barrier）**：执行被切成一连串超步，每个超步分三相：
  1. **Plan**：检查 channel 版本，被上一超步更新的 channel 所订阅的节点变为 active；条件边由路由函数决定激活谁；
  2. **Execute（并行）**：active 节点并行执行，**读隔离**——每个节点读的是超步开始时的状态快照，看不到同伴本步的写；**写缓冲**——输出先缓存不立即生效；
  3. **Update + Barrier + Checkpoint**：收集缓冲写 → 应用 reducer → channel 版本 +1 → **把整份状态序列化落 checkpoint**；屏障抬起才进入下一超步。

这套"计算—通信—屏障"的节奏，天然消灭了一大类竞态，也把"环"变成一等公民（ReAct 的 Think→Act→Observe 就是不断进行的超步序列），而 DAG 只能靠递归/外层循环间接表达环。

**核心机制的产品含义**（逐条对应本 PRD）：

- **Checkpointer = 逻辑时钟**：checkpoint 同时存 `channel_values`（用户数据）与 `channel_versions`（同步元数据），因此支持**时间旅行**：加载任一历史 checkpoint、重放、或从过去某点 fork 新分支。→ 对应 PRD"每步状态可见、可回退"。
- **`interrupt()` = 天然暂停点**：在标准 Python 里"暂停 await 并序列化挂起上下文"极痛苦；而 BSP 的**超步屏障本身就是暂停点**——配置中断后，运行时只是在屏障处停止调度、持久化状态、退出；恢复只需"重载 checkpoint → 继续下一超步"。→ **这正是本产品"等用户去 12306 买票，几小时/几天后回来"的定义性能力，且是框架原生，不需外挂 Temporal。**

**代码形态**（本产品两段式的骨架）：

```python
from typing import TypedDict, Literal
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command

class TripPlanState(TypedDict):
    stage: Literal["explore", "await_booking", "confirm"]
    constraints: dict
    candidate_cities: list[dict]
    transport_options: dict
    bookings: list[dict]
    bundle: dict

def await_booking_node(state: TripPlanState):
    explore_bundle = compose_explore_bundle(state)     # 先产出探索版
    user_bookings = interrupt({                         # 持久化并交回控制权给用户
        "type": "await_booking",
        "explore_bundle": explore_bundle,
    })
    return {"bookings": user_bookings, "stage": "confirm"}

graph = build_graph().compile(checkpointer=postgres_saver)
# 几天后用户回填 → 从 Postgres 检查点恢复，无需重跑前四步
graph.invoke(Command(resume=confirmed_bookings),
             config={"configurable": {"thread_id": plan_id}})
```

**支持的进阶模式**：动态扇出（`Send` 对象，运行时才知道批量大小的 Map-Reduce）、子图（把编译后的图当作父图的一个节点，实现分形组合与上下文隔离）、HITL（冻结世界→人改状态→恢复）。

**优势**：① 最强的**状态控制、持久化、可回退**；② 确定性、可审计、token 高效（每节点聚焦提示词，实测同类任务约 2000 token，远低于对话式的 8000）；③ 生态成熟（LangSmith 可观测、Postgres/Redis checkpointer）；④ 环、并行、人在环都是原生。

**劣势**：① **学习曲线陡**——必须理解 BSP 超步/channel/reducer，否则会踩"节点被调度两次"这类隐坑（真实案例：并行分支汇聚到同一下游节点时需显式加"屏障节点"）；② 依赖体量较重、版本演进快，升级需谨慎；③ 生态偏 Python（JS 版略滞后）；④ 对"简单线性流程"是杀鸡用牛刀。

**适用场景**：需要精确控制、合规审计、复杂分支、跨会话持久化、人在环的**生产级**工作流。**这正是本产品的画像。**

**扩展性/效果**：换模型（LangChain provider 抽象一行切换）、加工具（MCP/自定义）、水平扩容（Planner 无状态、状态在 Postgres checkpoint，直接横向扩）都成熟。生产实测常见评价：*"选它不是因为角色抽象更好写，而是显式状态图让给每个节点挂 OpenTelemetry span、排查线上静默失败最容易。"*

---

### 4.2 Microsoft Agent Framework（AutoGen + Semantic Kernel 的合流）

**定位**：微软 2025-10 宣布、2026-04-03 Python 与 .NET **同时 1.0 GA** 的统一编排 SDK，是 **AutoGen 与 Semantic Kernel 的正式继任者**（两者转入至少一年的维护期，提供迁移助手）。

**架构与设计**：融合了 AutoGen 的**对话式多智能体**抽象 + Semantic Kernel 的**企业特性**（基于会话的状态管理、中间件、遥测、类型安全），并**新增基于图的工作流（graph-based workflows）**，提供对多智能体执行路径的显式控制（类型安全路由、checkpointing、人在环）。开箱内置多种编排模式：**顺序、并发、handoff、群聊、Magentic-One**。

**代码形态**（概念示意，呈现"图工作流 + 编排模式"双层）：

```python
# pip install agent-framework  （Python）；.NET 为 Microsoft.Agents.AI
# 单体 Agent
agent = chat_client.create_agent(instructions="...", tools=[...])
# 多智能体：内置 sequential / concurrent / handoff / group-chat / Magentic 模式
workflow = (WorkflowBuilder()
            .add_edge(researcher, writer)
            .add_edge(writer, reviewer)
            .build())
```

**优势**：① 微软栈一等公民（Azure AI Foundry 可观测 + 负责任 AI 护栏：任务遵循、PII 保护、提示注入防御）；② **Python + .NET 双运行时**，对 C#/企业团队友好；③ 声明式 YAML 配置便于版本化部署；④ MCP 原生 + A2A（beta 适配包）；⑤ 图工作流已具备 checkpoint 与 HITL，能力向 LangGraph 靠拢。

**劣势**：① **新**（GA 才几个月），社区反馈的问题集中在"顺序上下文处理、函数审批范围"等编排取舍，以及**Azure OpenAI 快乐路径之外的 provider 适配**（非 Azure 基础设施要充分验证）；② 对话式基因带来 token 与状态分散的历史包袱；③ 若不在微软栈，得不到其最大红利。

**适用场景**：已重度投入 Azure/.NET 的企业；正在用 AutoGen/SK 需平滑迁移的团队。

**扩展性/效果**：企业级可观测（OTel 原生，可导出到 LangSmith）与治理是强项；但对本产品而言，"我们不在微软栈、且要 Qwen/DeepSeek/GLM + BYO Key"使其红利大打折扣。**它是 LangGraph 最有力的第二名，但落在了错误的生态上。**

---

### 4.3 OpenAI Agents SDK（极简原语）

**定位**：OpenAI 2025-03 发布的**轻量、低抽象**多智能体 SDK（Swarm 的生产级继任者），2026-04 加入**原生 sandbox 执行**，从"Completions 的包装"升级为完整执行 harness。

**架构——五个原语**：

1. **Agent**：配了 instructions/tools/handoffs 的 LLM，是计算的基本单元（不玩 CrewAI 那种 persona/backstory，直接把 instructions 当系统提示）。
2. **Runner**：执行器，管理"工具调用循环"，返回结构化结果（`final_output`、`last_agent`、`new_items` 全量回合、输入/输出护栏结果）。
3. **Handoff**：多智能体机制——**把"交给另一个 Agent"表示为一个工具**，LLM 在运行时自己决定调用 `transfer_to_xxx`；支持 `input_type` 传结构化载荷、`on_handoff` 回调。
4. **Guardrail**：输入/输出/工具调用的校验，可阻断（tripwire）或并行。*官方最强特性之一*：用便宜小模型（如 mini）跑输入护栏，先过滤越界/恶意请求再调贵模型，省 60–80% token。
5. **Session**：跨回合记忆，抽象基类 + SQLite/Redis/加密实现，换后端很容易。

**代码形态**：

```python
from agents import Agent, Runner, handoff, function_tool

@function_tool
def lookup_order(order_id: str) -> str: ...

triage = Agent(name="Triage", instructions="路由到合适的专家",
               handoffs=[handoff(refund_agent, tool_name_override="transfer_to_refunds")])
result = await Runner.run(triage, "我要退款…")
print(result.last_agent.name, result.final_output)
```

**优势**：① **极简、依赖少、易推理**，"agent 是对象、handoff 是函数调用、guardrail 是校验函数"；② handoff + 结构化 I/O 是同类里最干净的委派模式；③ 内置 tracing；④ MCP 消费者；⑤ 对 OpenAI 模型有 Responses API 原生优化（更低延迟、更好工具调用）。

**劣势**：① **持久化短板**——Session 只是"多回合记忆"，**跨进程重启的工作流级持久化需外挂 Temporal/DBOS**，无法原生表达"跨天中断恢复"；② **OpenAI 中心**，非 OpenAI 模型要靠 LiteLLM 扩展、非一等公民；③ 抽象是"agent 形状"而非"工作流/DAG 形状"，复杂分支不如图模型。

**适用场景**：紧耦合 OpenAI 模型、范围收敛的助手、干净的委派型工作流、看重执行清晰度胜过特性广度。

**扩展性/效果**：sandbox（Docker/Unix）+ guardrail 让"读文件、跑命令、产物落地"可在边界内自建 harness；但 sandbox 冷启动 2–4s、Docker socket 多租户风险需注意。**对本产品致命的是"跨天持久化要外挂 Temporal"——这恰是我们最核心的需求。**

---

### 4.4 CrewAI（角色团队 + Flows）

**定位**：独立的多智能体编排框架（**不依赖** LangChain），围绕"角色扮演的团队"心智模型，主打**上手快**。2024 年后补上 **Flows**（事件驱动的确定性编排），形成"Crews（自主协作）+ Flows（精确控制）"双轨。

**架构与设计**：

- **Crews 轨**：`Agent`（有 role/goal/backstory 人设 + tools）、`Task`（带 `expected_output` 和 `context` 依赖）、`Crew`（`Process.sequential` 或 `Process.hierarchical` —— 后者由一个 manager LLM 自动协调谁干什么、何时委派、如何综合）。
- **Flows 轨**：用 `@start`/`@listen` 装饰器把步骤连成事件驱动图，提供状态管理与更确定的控制，弥补纯 Crews 不可控的短板。

**代码形态**：

```python
from crewai import Agent, Task, Crew, Process

classifier = Agent(role="意图分类专家", goal="准确归类客诉", backstory="…多年客服经验…", tools=[...])
task = Task(description="分类：{msg}", expected_output="意图+紧急度", agent=classifier, context=[...])
crew = Crew(agents=[...], tasks=[...], process=Process.sequential, memory=True)
result = crew.kickoff(inputs={"msg": "我的账单错了！"})
```

**优势**：① **心智模型直观**、样板代码少、原型速度快，非工程师也能理解；② 内置 memory/learning、丰富的 `crewai_tools`（搜索/抓取/文件/代码解释器）；③ MCP 全量客户端支持（stdio/SSE/HTTP）；④ 本地模型（Ollama）友好。

**劣势**：① **细粒度状态控制与严格回退弱**——"委派"后主控 agent 往往拿不回控制权、难以逐步审计；② 生产反馈显示 LLM 生成的工具调用可能与实际执行不符（行动轨迹失真），异步执行与前端流式是痛点；③ backstory 增加 token 开销（实测约 3500 token，高于 LangGraph）；④ 非 OpenAI provider 与 memory 连接是社区高频摩擦点，升级易破坏集成；⑤ 免费额度仅 50 次/月，非平凡生产要走企业合同。

**适用场景**：工作流能自然映射为"一队专家 + 清晰任务边界"的自动化（邮件分诊、内容流水线、调研），且底座模型工具调用可靠。

**扩展性/效果**：加工具/接 MCP 都方便，但"角色抽象好写、却难以插桩排障"是生产共识。**对本产品：证据优先要求"每个字段来源可控、可回退"，角色扮演的涌现式协作恰恰相反——不合适。**

---

### 4.5 LlamaIndex Workflows（事件驱动）

**定位**：LlamaIndex 的**事件驱动编排层**（Workflows 1.0 已稳定），用纯 Python 把 Agent 执行建模为"一组事件处理器（step）"，无需单独 DSL。**文档密集/数据密集**场景是其主场。

**架构与设计**：每个 `@step` 方法**接收一个事件、发射一个事件**，`StartEvent` 触发、`StopEvent` 终止，`Context` 承载共享状态（可序列化以支持 HITL 与恢复）。步骤间靠事件类型自动连边，天然支持嵌套/并行管线。与 LlamaIndex 的数据加载/检索、以及商用 **LlamaParse（OCR/文档抽取）** 无缝衔接。

**代码形态**：

```python
from llama_index.core.workflow import Workflow, step, StartEvent, StopEvent, Event

class RetrieveEvent(Event):
    chunks: list

class RAGFlow(Workflow):
    @step
    async def retrieve(self, ev: StartEvent) -> RetrieveEvent:
        return RetrieveEvent(chunks=await retriever.aretrieve(ev.query))
    @step
    async def synthesize(self, ev: RetrieveEvent) -> StopEvent:
        return StopEvent(result=await llm.acomplete(build_prompt(ev.chunks)))
```

**优势**：① 事件模型清爽、可组合可观测、无需专门编排服务器（脚本/笔记本/FastAPI 中间件即可跑）；② **文档解析/检索生态最强**（LlamaParse 直喂管线）；③ Python 一等（TS 版 `workflows-ts` 已弃用，官方引导回 Python）。

**劣势**：① `AgentWorkflow` 高层抽象有**交接失败**（接收 agent 停止响应）等生产风险；② 可观测在并发下有丢 span/部分 trace 的问题；③ 事件驱动写法样板较多；④ 脱离 LlamaIndex 数据生态时，性价比不如 LangGraph。

**适用场景**：文档中心、数据密集的多智能体系统；已重度使用 LlamaIndex 检索栈的团队。

**扩展性/效果**：作为**编排器**它不如 LangGraph 的持久化/回退成熟；但它的**文档解析件**（LlamaParse/Reader）对本产品的"活动情报流水线"是有用的**上游组件**——这正是我们打算"借件不借编排器"的地方（见第 9 节）。

---

### 4.6 Google ADK（电池全含，GCP 原生）

**定位**：Google 的**主张鲜明、电池全含**的 Agent 开发框架（Apache 2.0，~19k stars），目标是在 Google Cloud 上快速构建、调试、部署 Agent。

**架构与设计**：内置会话管理、浏览器调试 UI（**ADK Web**）、代码执行、CLI（`adk run`/`adk api_server` 免写服务器样板）。多智能体原生：`LlmAgent` + `SequentialAgent`/`ParallelAgent`/`LoopAgent` 组合，`sub_agents` 委派。部署直达 Cloud Run / GKE / Vertex AI Agent Engine，深度集成 IAM/Pub-Sub/BigQuery。协议上支持 MCP、A2A、OpenAPI。

**优势**：① **开发者体验好**（CLI + 调试 UI + 代码执行开箱）；② GCP 部署一条龙；③ 号称模型无关，实则 Gemini/Vertex 最顺。

**劣势**：① **强 GCP 绑定**——离开 Cloud Run/Vertex，电池红利骤减，需自建部署与状态层；② 会话持久化有坑（Cloud Run 容器重启丢内存态、持久化配置不当曾致跨用户串数据），生产前须显式配外部存储并验证隔离。

**适用场景**：GCP 原生团队要"开箱即用的 Agent 运行时 + 内建调试"。

**扩展性/效果**：GCP 内扩展顺滑，GCP 外要自己搭桥。**对本产品：我们不在 GCP、且要中立多云 + 国内模型——直接排除。**

---

### 4.7 PydanticAI（类型安全优先）

**定位**：Pydantic 团队出品的 Python Agent 框架，把 **Pydantic 的类型安全与校验**带入 Agent 开发；配套 `pydantic-graph` 做图编排，并原生集成 **Temporal / DBOS** 做**持久化执行（durable execution）**。

**架构与设计**：`Agent(model, deps_type=..., output_type=SomeModel)` —— 输出直接是**校验过的 Pydantic 模型**；`@agent.tool` + `RunContext[Deps]` 做依赖注入的工具；复杂流程用 `pydantic-graph` 的 `BaseNode`/`GraphRunContext`。持久化不是自带 checkpointer，而是**把 agent 包进 Temporal workflow**，靠 Temporal 的确定性重放获得跨故障/重启的持久性。

**代码形态**：

```python
from pydantic import BaseModel
from pydantic_ai import Agent, RunContext

class BookingExtract(BaseModel):
    train_no: str; depart: str; arrive: str; price: float | None

agent = Agent("qwen:qwen-plus", output_type=BookingExtract)  # 输出即校验模型
result = agent.run_sync("识别这段车票文本…")
booking: BookingExtract = result.output   # 类型安全、IDE 补全、错误前移
```

**优势**：① **类型安全 + 结构化输出**是同类最佳，天然承载"结构化事实 + 校验"；② 由 Pydantic 团队维护（OpenAI SDK、Anthropic SDK 都在用 Pydantic 校验层），工程质量高；③ 模型无关、依赖轻；④ durable execution 有正统方案（Temporal/DBOS）。

**劣势**：① **持久化依赖外部**（Temporal/DBOS），运维更重，不如 LangGraph checkpointer 开箱；② `pydantic-graph` 生态与成熟度不及 LangGraph；③ 图编排的时间旅行/HITL 不是其强项。

**适用场景**：以"可靠的结构化输出"为核心的抽取/校验型任务；愿意上 Temporal 换取工业级持久化的团队。

**扩展性/效果**：**对本产品最有价值的用法是"配角"**——在 LangGraph 节点内部，用 PydanticAI 做"回填抽取 / 活动字段抽取"的 LLM 子调用，让输出直接是校验过的 `Fact/Evidence` 模型，与 Provenance Guard 严丝合缝（见第 9 节）。

---

### 4.8 轻量与专用：Agno / smolagents / Strands / Mastra

这一组各有鲜明取向，但都**不适合做本产品的核心编排器**，列出以证明"我们看过并排除了"。

| 框架 | 定位与架构 | 亮点 | 为何不作核心 |
|---|---|---|---|
| **Agno**（原 Phidata） | 高性能 Agent 运行时 + AgentOS；`Agent`/`Team`/memory/knowledge | **极致性能**：实例化 ~3μs、内存 ~6.6KiB；多模态、结构化输出好 | 强项是"高并发轻量 Agent"，非"跨天持久化状态机"；持久化/回退非其卖点 |
| **smolagents**（HuggingFace） | 极简 **CodeAgent**：LLM **把动作写成 Python 代码**并执行（比 JSON 工具调用更紧凑高效） | 几行起一个 code agent；开源、轻 | **模型驱动、代码即动作**与"证据优先、禁止模型自由发挥"直接冲突；沙箱安全负担 |
| **Strands Agents**（AWS） | **模型驱动**的 agentic loop，让现代 LLM 自己驱动行为；`Agent(model, tools)` | 少代码、自适应强、AWS 集成 | 把控制权交给模型 = 反"确定性护栏"；不合规审计诉求 |
| **Mastra**（TypeScript） | TS 优先、电池全含（workflow/memory/Studio），Vercel/Next.js 友好 | **TS 全栈**体验最佳、前端集成顺 | 我们的规划服务是 **Python**（抓取/OCR/OR-Tools 生态在 Python）；TS 编排无法覆盖数据脏活 |

**一句话**：Agno 赢在性能、smolagents/Strands 赢在自治与极简、Mastra 赢在 TS 体验；但本产品要的是**确定性控制 + 跨天持久化 + Python 数据生态**，它们都不在这条主轴上。

---

### 4.9 Harness 类：Claude Agent SDK / Deep Agents

**定位**：这是与"编排框架"正交的一类——**长时程自治 Agent 的"外壳/骨架"**，专为编码、深度研究等**开放式、长跑**任务设计。

- **Claude Agent SDK**（Anthropic）：官方 harness，核心是 **hooks（在工具调用前/响应后/出错等生命周期点拦截）+ subagents + 自动上下文压缩**（接近 200k 上限时自动摘要历史，据报有显著性能提升）；内建读文件/跑命令/搜网 + MCP。**强 Claude 绑定**。
- **Deep Agents**（LangChain）：模型无关（100+ 模型）的通用 Agent harness，处理规划、上下文管理、多智能体编排，用于研究/编码等长跑复杂工作，与 LangGraph 同源。

**优劣**：① 优势是"开放式长任务"的上下文工程与自治规划做得深；② 但**自治=不可控**，与本产品"证据优先、每步可审计、禁止编造交通/票价"的硬约束相悖。

**对本产品的启示**：我们的"当周活动即时调研"确实是一种"深度研究"，但**必须是带护栏的有界研究**——用 LangGraph 子图 + 搜索工具 + Provenance Guard 实现，而**不是**放一个开放式 deep agent 去自由发挥。harness 的"上下文压缩/子代理"思想可借鉴，但不采用其自治外壳。

---

### 4.10 低代码平台：Dify / Coze

**定位**：可视化拖拽的 Agent/工作流平台，面向"快速搭建、少写代码"。

- **Dify**：开源、可自托管，可视化编排 + RAG + 工具，**厂商锁定风险相对低**（自托管 + 模型/工具可移植）。
- **Coze**（字节）：生态丰富、上手极快，但**专有生态、切换成本高**。

**优劣**：① 优势是**极速搭原型**、非工程师可用、集成现成；② 但对本产品是**黑盒**——难以表达"证据六态、字段级来源白名单、Provenance Guard 三闸、跨天 interrupt 回退"这类复杂且需强控制的逻辑，且平台锁定与既有技术方案的判断一致（既有文档已将其判为"❌ 不适合作为核心"）。

**适用场景**：内部工具、营销/客服 bot、快速验证概念。**对本产品：可作为"运营侧内部小工具"（如活动源人工审核台的辅助），但绝不作为核心编排。**

---

## 5. 横向对比矩阵

> 评分口径：★★★★★ = 同类最强；— = 不适用/非其设计目标。“本产品契合度”是关键列。

| 框架 | 架构范式 | 跨天持久化/恢复 | 人在环(interrupt) | 可回退/时间旅行 | 确定性控制 | 结构化输出护栏 | 模型中立 | Python 生态 | 学习曲线 | token 效率 | **本产品契合度** |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **LangGraph** | 图/状态机(BSP) | ★★★★★ 原生 | ★★★★★ 原生 | ★★★★★ | ★★★★★ | ★★★★(配 Pydantic) | ★★★★★ | ★★★★★ | ★★(陡) | ★★★★★ | **★★★★★ 首选** |
| **MS Agent Framework** | 图工作流+对话 | ★★★★(checkpoint) | ★★★★ | ★★★ | ★★★★ | ★★★★ | ★★★★ | ★★★★(.NET也强) | ★★★ | ★★★★ | ★★★ 次选(非本栈) |
| **OpenAI Agents SDK** | 极简原语+handoff | ★★(需外挂Temporal) | ★★ | ★★ | ★★★ | ★★★★★(guardrail) | ★★(OpenAI中心) | ★★★★ | ★★★★★ | ★★★★ | ★★ |
| **CrewAI** | 角色团队+Flows | ★★ | ★★ | ★★ | ★★ | ★★ | ★★★ | ★★★★ | ★★★★★ | ★★(backstory贵) | ★★ |
| **LlamaIndex Workflows** | 事件驱动 | ★★★(Context序列化) | ★★★ | ★★★ | ★★★★ | ★★★ | ★★★★ | ★★★★★ | ★★★ | ★★★★ | ★★★(仅解析件) |
| **Google ADK** | 层级Agent+GCP | ★★★(GCP内) | ★★★ | ★★ | ★★★ | ★★★ | ★★★(Gemini最顺) | ★★★★ | ★★★ | ★★★ | ★(非GCP) |
| **PydanticAI** | 类型安全+graph | ★★★★(Temporal) | ★★★ | ★★ | ★★★★ | ★★★★★ | ★★★★★ | ★★★★★ | ★★★★ | ★★★★ | ★★★★ 配角 |
| **Agno** | 高性能运行时 | ★★★ | ★★ | ★★ | ★★★ | ★★★★ | ★★★★ | ★★★★ | ★★★★ | ★★★★★ | ★★ |
| **smolagents/Strands** | 模型驱动自治 | ★ | ★ | — | ★(故意交模型) | ★★ | ★★★★/★★(AWS) | ★★★★ | ★★★★★ | ★★ | ★(反确定性) |
| **Mastra** | TS 图/工作流 | ★★★★ | ★★★ | ★★★ | ★★★★ | ★★★ | ★★★★ | —(TS) | ★★★★ | ★★★★ | ★★(非Python) |
| **Dify/Coze** | 低代码黑盒 | ★★ | ★★ | ★★ | — | ★★ | ★★★ | ★★★★ | ★★★★★ | — | ★(黑盒锁定) |

**矩阵读法**：本产品的硬需集中在前四列（跨天持久化、interrupt、可回退、确定性控制）。**只有 LangGraph 在这四列同时拿满星且为原生能力**；MAF/Mastra 接近但分别输在生态（非本栈）与语言（非Python）；OpenAI SDK/PydanticAI 需外挂 Temporal 才能补齐跨天持久化。

---

## 6. 五种架构范式的本质区别

拨开品牌，所有框架归为五种底层范式，选型本质是**选范式、再选实现**：

1. **图/状态机（LangGraph、MAF 图工作流、Mastra）**：状态是中心，转移是工作。**控制权在开发者**。适合需要审计、回退、持久化的生产流程。→ **本产品属于此类。**
2. **事件驱动（LlamaIndex Workflows）**：步骤收发事件，松耦合。介于图与对话之间，文档管线强。
3. **角色/团队（CrewAI、Agno Team）**：把问题映射为人类团队分工。上手快，但细粒度控制弱。
4. **对话式（AutoGen，已合流入 MAF）**：Agent 互发消息直到收敛。创造性/迭代强，但 **token 最贵、状态分散、最难审计**。
5. **模型驱动自治 / Harness（Strands、smolagents、Claude Agent SDK、Deep Agents）**：**控制权交给模型**。自适应强，但不可预测、难护栏。

**一个反直觉但关键的结论**：很多人把“多智能体”当先进，但本产品**不是一个对话式多智能体系统**，而是一个**嵌入了少量 LLM 调用的确定性工作流**：门到门交通、时间线求解、预算都是确定性计算，只有“约束解析、活动抽取、取舍解释”才用模型。选“图/状态机”而非“角色/对话”，是因为后者会把一个本可控的流程变成不可控的涌现。

---

## 7. 互操作协议：MCP / A2A / AG-UI

2026 年，框架竞争重心转向“协议互通”。三个关键协议：

- **MCP（Model Context Protocol，Anthropic 提出）**：标准化 **Agent ↔ 工具/数据源** 的连接。已成事实标准，几乎所有主流框架（LangGraph、MAF、OpenAI SDK、CrewAI、ADK、Mastra…）都原生支持。**对本产品价值巨大**：把高德地图/和风天气/VariFlight 航班/搜索 API 封装为 MCP Server，则编排层与工具层解耦，对齐 PRD v0.3“开放数据源插件机制”，浏览器扩展也可复用同一套工具定义。
- **A2A（Agent2Agent，Google 提出→Linux Foundation 托管）**：标准化 **Agent ↔ Agent** 的协作（Agent Card 发现能力、任务委派）。**本产品初期用不上**（我们是单一规划服务，非跨组织多 Agent）；但可作为未来“城市编辑者/社区贡献 Agent”的预留接口。
- **AG-UI（Agent-User Interaction）**：标准化 **Agent ↔ 前端** 的流式交互（事件流、状态同步、HITL 交互）。对本产品的 SSE 流式卡片/回填交互有参考价值，但非必选。

**选型涵义**：**拥抱 MCP 作为工具/数据连接标准**（与既有方案的“统一 Provider 抽象”合流：Provider 内部实现可以是 MCP client）；A2A/AG-UI 作为路线图预留，不在 v0.1 引入。

---

## 8. 回到本 PRD：需求画像 → 框架能力映射

把 PRD/技术方案的**硬约束**逐条映射到框架能力，是选型的唯一正确方法（而非比 star 数）：

| 本产品硬需求（来自 PRD/技术方案） | 对框架的要求 | 能满足的框架 |
|---|---|---|
| **两段式规划**：用户离开去 12306/航司买票，几小时/几天后回来继续 | 跨天可中断、可持久化、可恢复 | **LangGraph（原生）**、MAF、PydanticAI+Temporal、OpenAI SDK+Temporal |
| **每步状态可见、可回退** | 状态快照 + 时间旅行 | **LangGraph（checkpoint 最佳）** |
| **人在环**：等回填作为一等公民 | 原生 interrupt/resume | **LangGraph（interrupt）**、LlamaIndex、Mastra |
| **证据优先/防幻觉**：字段级来源白名单、Provenance Guard、未确认误展=0 | 确定性控制 + 结构化输出校验 | LangGraph(控制) + **PydanticAI/Pydantic(校验)** |
| **确定性计算为主**：门到门交通、时间线求解、预算 | LLM 只是节点内子调用，非驾驶者 | 图/状态机类（LangGraph 最佳） |
| **Python 生态**：抓取(Playwright)、OCR(Qwen-VL)、OR-Tools、pgvector | Python 一等公民 | LangGraph、LlamaIndex、PydanticAI、CrewAI（非 Mastra） |
| **中立 + BYO Key**：通义/DeepSeek/GLM + 用户自带 Key | 模型中立、多 provider | LangGraph、MAF、PydanticAI（非 OpenAI SDK/ADK 单一生态） |
| **轻后端 + 成本可控** | token 高效、无状态横扩 | LangGraph（节点聚焦提示词、状态在 Postgres） |
| **开放数据源插件机制（v0.3）** | 工具/数据解耦标准 | **MCP** |

**结论**：逐行看下来，LangGraph 是唯一在**每一条硬需求**上都落在“原生/最佳”列的框架；其他候选要么在核心的“跨天持久化”上需外挂，要么输在生态/语言/中立性上。

---

## 9. 技术选型建议

### 9.1 主选：LangGraph 作为核心编排（验证既有决策）

本次独立横评的结论与既有技术方案**一致**：核心规划流用 **LangGraph（Python）+ Postgres Checkpointer**。但本文给出的是**反向验证**的理由：把整个赛道摊开后，**没有第二个框架能在不外挂额外系统的前提下，同时满足“跨天中断恢复 + 可回退 + 确定性控制 + Python 生态”。

### 9.2 配套选型（分层，而非单一框架打天下）

| 层 | 选型 | 在本产品里具体干什么 |
|---|---|---|
| 核心编排 | **LangGraph** | 10 节点状态机、`interrupt` 两段式、checkpoint 恢复、可回退重规划 |
| 结构化抽取/护栏 | **PydanticAI 或 Pydantic + Function Calling** | 在 LangGraph 节点内做回填抽取/活动字段抽取，输出即校验过的 `Fact/Evidence`；预算小则用 Pydantic 即可，不引入新依赖 |
| 工具/数据连接 | **MCP** | 地图/天气/航班/搜索封装为 MCP Server；Provider 抽象层作为 MCP client |
| 文档解析（可选） | **LlamaParse / Reader 解析件** | 仅用于活动情报流水线的网页→干净正文，**不**引入其编排器 |
| 排程求解 | 确定性启发式 + （v0.2+）OR-Tools | 周末规模小，贪心排点 + 硬约束校验即可，不上来就上重型求解器 |
| 可观测 | **LangSmith 或 Langfuse（OTel）** | 为证据 KPI（未确认误展=0）提供埋点与回放；框架无关 |

### 9.3 明确排除项与理由

- ❌ **对话式多智能体（AutoGen）/ 角色扮演（CrewAI）**：涌现式协作与“证据优先、字段级可控可回退”相悖；token 贵、状态分散。
- ❌ **模型驱动自治（Strands/smolagents）**：把控制权交给模型 = 反护栏，禁止编造交通/票价的硬约束无法保证。
- ❌ **低代码黑盒（Dify/Coze）**：无法表达证据六态/三闸/跨天回退；锁定。（仅允许作运营内部小工具）
- ❌ **云绑定运行时（Google ADK/GCP）**：与中立多云 + 国内模型冲突。
- ⚠️ **OpenAI Agents SDK / MAF 作为核心**：前者跨天持久化要外挂 Temporal 且 OpenAI 中心；后者优势在微软栈——都非最优，但可作为**备选**（若未来团队技术栈迁移）。

### 9.4 分层架构一张图

```text
前端 Next.js PWA ──SSE─→ BFF(Node) ──→ Planner Service (Python/FastAPI)
                                              │
                                   ⌈──── LangGraph 图状态机 ────⌉
                                   │  节点内调用 PydanticAI 抽取   │
                                   │  interrupt() ↔ Postgres      │
                                   └─────────────────────────┘
                                              │ 统一 Provider 抽象 = MCP client
                        高德地图·和风天气·VariFlight·搜索 (均可 MCP Server 化)
```

---

## 10. 选型落地要点与风险

**落地要点**：
1. **把 BSP 心智模型写进团队规范**：并行分支汇聚到同一下游节点时，显式加“屏障节点”，避免“节点被调度两次”的隐坑（本文 4.1 已记录真实案例）。
2. **Provenance Guard 与 LangGraph 解耦**：护栏是纯函数断言，在 `PlanComposer` 节点前跑，进 CI 单测（与既有方案第 7 节一致），不依赖任何框架特性。
3. **checkpoint 生命周期管理**：长周期 interrupt 会使状态膨胀，需 checkpoint TTL + 过期计划归档。
4. **MCP 化逐步推进**：v0.1 可先用普通 Provider 封装，待工具增多再改造为 MCP Server，避免过度工程。
5. **版本锁定**：LangGraph 演进快，锁定 1.x minor 版本并建回归测试，升级走灰度。

**风险与缓解**：

| 风险 | 影响 | 缓解 |
|---|---|---|
| LangGraph 学习曲线陡，团队踩坑 | 中 | 内部示范图 + code review 检查 BSP 拓扑；先写确定性节点后接 LLM |
| 依赖体量重、升级易碎 | 中 | 锁定版本 + 容器化 + 回归测试；Planner 与抽取层解耦 |
| 过度依赖单一框架生态 | 低中 | 确定性服务/护栏/求解器均为纯 Python，不绑定 LangGraph，可迁移 |
| 未来若转 TS 全栈 | 低 | 届时可评估 Mastra/LangGraph.js；但数据脏活仍建议留 Python |

---

## 11. 附录：速查表

**一句话选框架**：

- 要**跨天持久化状态机 + 人在环 + 可回退**（本产品）→ **LangGraph**
- 在**微软/Azure/.NET 栈** → Microsoft Agent Framework
- **紧耦合 OpenAI、极简委派** → OpenAI Agents SDK
- **角色团队、快速原型** → CrewAI
- **文档密集、事件驱动** → LlamaIndex Workflows
- **GCP 原生** → Google ADK
- **类型安全结构化输出** → PydanticAI
- **极致性能轻量 Agent** → Agno
- **TS 全栈** → Mastra
- **编码/长跑自治任务** → Claude Agent SDK / Deep Agents
- **非工程师快搭** → Dify / Coze

**本产品最终选型一行总结**：

> **LangGraph（核心编排）+ PydanticAI/Pydantic（结构化护栏）+ MCP（工具连接）+ LangSmith/Langfuse（可观测），以 LlamaParse 解析件为可选上游。“图/状态机 + 确定性计算 + 分层 LLM + 防幻觉护栏”——这条主轴与既有技术方案完全一致，并经全赛道横评反向验证成立。**

---

> 本文信息截至 2026 年 7 月；框架版本、星数、定价与能力会变化，具体选型前请以官方文档为准。
