# DD-16 记忆与个性化（会话记忆 + Mem0 长期记忆）· 详细设计

**详细设计系列 · v2 新增能力 · v1.0 · 2026 年 7 月**

> 本文定义 v2 的**记忆层**：让系统**跨会话察觉用户的喜好与历史**（"你上次从上海出发、爱看展、不吃辣、去过杭州"），并在本次对话开始即注入、结束时更新。它把 v1.1 延后到 v0.2 的记忆能力**提前到 v0.1**。
>
> **上游依据**：v2 增补 D2；`03 记忆框架调研`（Mem0 语义记忆、ADD-only 覆盖预警、三层记忆分类）；v1.1 增补 C（记忆分层路线）；DD-01（`user_context`、pgvector）；DD-04（Mem0 客户端 / embedding）；DD-15（会话开始注入、结束回写）；DD-05（偏好参与重排）。
> **下游消费者**：DD-15 对话 Copilot（注入/回写）、DD-07 约束澄清（缺省来源）、DD-05 检索（偏好重排）、DD-08 目的地（偏好加权）。
> **一句话**：**会话内靠 checkpoint，会话间靠 Mem0；写入带"覆盖语义"，避免旧偏好、新偏好同时被召回。**

---

## 1. 模块职责与边界

| 项 | 说明 |
|---|---|
| **职责** | ① 三层记忆的存取；② 会话开始**召回并注入**长期偏好/历史；③ 会话中/结束**抽取并写入**新偏好（带覆盖语义）；④ 供检索/澄清/打分**个性化**。 |
| **边界内** | 记忆数据模型（`user_memory` + `user_context` 扩展）、`load_memory`/`write_memory`/`search_memory` 接口、覆盖语义、隐私（可查可删）、注入策略。 |
| **边界外** | 会话编排（DD-15）、检索算法（DD-05）、约束结构化（DD-07）、embedding/LLM 底座（DD-04）。 |
| **架构位置** | 横切个性化层；被 DD-15 在会话生命周期两端调用。 |

---

## 2. 三层记忆模型（对齐记忆框架调研）

| 层 | 内容 | 方案 | 落地 |
|---|---|---|---|
| **会话工作记忆** | 本次对话的多轮消息、已否决项、当前约束 | **LangGraph checkpoint**（已有，DD-02）+ `state.conversation` | v0.1 ✅ |
| **长期偏好（语义）** | 常用出发地、预算区间、口味/忌讳、活动兴趣、飞机/高铁偏好 | **Mem0 风格语义记忆**（`user_memory` 表 + pgvector） | v0.1 ✅（本文） |
| **跨会话演化（时序）** | "搬家了""孩子长大了""季节偏好变化" | Graphiti 双时间轴（可溯源） | v0.3 视需要（预留） |

> v0.1 落地前两层；第三层预留（`user_memory` 的软失效 + `valid_from/to` 可平滑演进到时序）。

---

## 3. 数据模型（新增，需回改 DD-01）

```sql
-- 长期语义记忆（Mem0 风格；也可直接用 Mem0 库 + pgvector 后端，schema 对齐本表）
CREATE TABLE user_memory (
  id          BIGSERIAL PRIMARY KEY,
  user_id     BIGINT REFERENCES users(id) ON DELETE CASCADE,
  mem_type    TEXT NOT NULL,              -- preference / fact / episodic
  key         TEXT,                       -- 归一化键(diet/origin_city/budget/interest)用于覆盖
  content     TEXT NOT NULL,              -- 自然语言记忆（"偏好看展览、不吃辣"）
  embedding   VECTOR(1024),               -- 语义召回（BGE-M3）
  confidence  REAL DEFAULT 0.7,
  source_plan_id BIGINT,                  -- 来自哪次会话（可溯源）
  valid       BOOLEAN DEFAULT TRUE,       -- 覆盖时旧记忆置 FALSE（软失效、可回溯）
  created_at  TIMESTAMPTZ DEFAULT now(),
  updated_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON user_memory USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON user_memory (user_id, mem_type, valid);
```

> `user_context`（DD-01 §6.2）保留为**结构化快照**（常用出发地/预算/忌讳等强字段，供快速缺省）；`user_memory` 为**语义记忆**（可模糊召回的偏好/历史）。二者互补：结构化的走 `user_context`，语义的走 `user_memory`。

---

## 4. 覆盖语义（★ 解决 Mem0 ADD-only 的"旧新偏好共存"预警）

记忆框架调研明确：新版 Mem0 是 **ADD-only**，会让"旧偏好、新偏好同时被召回"（stale/contradictory）。我们**自建覆盖语义**：

```python
async def write_memory(user_id, mem_type, key, content, plan_id, conf=0.7):
    emb = await svc.embed([content])[0]
    if key:                                   # 有归一化键 → 同键覆盖
        await db.execute("""UPDATE user_memory SET valid=FALSE, updated_at=now()
                            WHERE user_id=$1 AND key=$2 AND valid=TRUE""", user_id, key)
    await db.execute("""INSERT INTO user_memory
        (user_id,mem_type,key,content,embedding,confidence,source_plan_id)
        VALUES ($1,$2,$3,$4,$5,$6,$7)""", user_id, mem_type, key, content, emb, conf, plan_id)
    # 无 key 的自由记忆：靠检索期"同簇取最新 + 阈值"去重（§5）
```

- **同键覆盖**（如 `key='diet'`："不吃辣" → 新填"能吃辣了"）：旧记录软失效、只召回最新；
- **无键记忆**：检索时按语义近邻聚簇，同簇**取最新 + confidence 阈值**过滤（不返回互相矛盾的旧值）；
- **软失效**（`valid=FALSE` 而非删除）：可回溯"曾经的偏好"，为 v0.3 时序演化预留。

---

## 5. 接口契约

```python
async def load_memory(user_id: str, query: str | None = None, top_k: int = 8) -> dict:
    """会话开始调用：返回 {structured: user_context, semantic: [记忆条目]}。
    query 非空则语义召回相关记忆；否则取高置信近期记忆。仅 valid=TRUE。"""

async def write_memory(user_id, mem_type, key, content, plan_id, conf=0.7): ...  # §4

async def extract_and_write(conversation: list[dict], plan: dict, user_id: str) -> list[int]:
    """会话结束/确认时调用：LLM 从对话抽取稳定偏好/事实（带 key 归一），
    经覆盖语义写入；返回写入的 memory ids。抽取只取'稳定信号'，不记一次性约束。"""
```

**注入点**（DD-15 会话开始）：`memory_ctx = load_memory(user_id, query=首轮消息)` → 注入意图分类/澄清 prompt + 作为澄清缺省（命中则不问）+ 传给 DD-05 做偏好重排。

---

## 6. 与其它模块接线

| 关系 | 说明 |
|---|---|
| **DD-15** | 会话开始 `load_memory` 注入 `memory_ctx`；结束 `extract_and_write` 回写 |
| **DD-07** | `user_context`/`user_memory` 作为澄清**缺省来源**（老用户免重复问，DD-07 §7 已述） |
| **DD-05** | `rerank_query` 拼入偏好（"爱看展/不吃辣"）→ 个性化重排（DD-05 §6） |
| **DD-08** | 目的地打分可对"去过的城"降权、对偏好品类加权 |
| **DD-04** | `embed`（记忆向量）；可选直接用 Mem0 库（pgvector 后端，schema 对齐 §3） |

---

## 7. 隐私与合规（PIPL，对齐 DD-01 §11）

- 记忆**显式授权**后才保存；进 LLM 前经 DD-04 `redact()`（精确位置/证件脱敏）；
- **可查可删**：用户可查看/删除任意记忆（`ON DELETE CASCADE` + 软失效可硬删）；
- 记忆内容脱敏粒度（商圈级、预算区间），不存精确门牌/证件；
- 多人会话中，他人偏好不写入组织者的长期记忆（隔离）。

---

## 8. 降级

| 失效 | 降级 |
|---|---|
| `user_memory` 召回失败/embedding 挂 | 退化为只用 `user_context` 结构化缺省；不阻塞会话 |
| 抽取 LLM 挂 | 会话结束不写语义记忆（仅更新结构化 `user_context`）；下次仍可用 |
| 新用户无记忆 | 冷启动正常问询（无个性化，符合预期） |

---

## 9. 效果与验收标准（DoD）

1. **跨会话生效**：老用户新会话，命中偏好的槽位不再追问、检索排序体现偏好（对照冷启动）。
2. **覆盖正确**：同键偏好更新后，只召回最新值，不返回矛盾旧值（覆盖语义用例）。
3. **不过度记忆**：一次性约束（"这次就 2 个人"）**不**写入长期记忆；稳定偏好（"我一般不吃辣"）才写（抽取精度用例）。
4. **隐私**：一键查看/删除记忆通过；记忆内容无精确门牌/证件。
5. **降级**：mock embedding 故障 → 退化结构化缺省仍可用。

---

## 10. 开发任务拆解 + 风险

**任务**：① `user_memory` 表 + 索引（0.5d）；② `write_memory` 覆盖语义 + `load_memory` 召回（1d）；③ `extract_and_write` 抽取（key 归一 + 稳定信号判定）（1.5d）；④ DD-15 注入/回写接线（1d）；⑤ DD-05/07/08 个性化接入（1d）；⑥ 隐私（查/删）+ 降级 + 验收（1d）。

| 风险 | 缓解 |
|---|---|
| 旧新偏好共存（Mem0 通病） | **自建覆盖语义**（同键软失效 + 同簇取最新，§4） |
| 过度记忆一次性信息 | 抽取只取"稳定信号"，一次性约束不入长期记忆 |
| 记忆污染个性化（误记偏好） | 置信度阈值 + 用户可删 + 冲突时以最新/最高置信为准 |
| 隐私合规 | 显式授权 + 脱敏 + 可查可删（DD-01 §11） |

---

## 11. 当前实现状态（2026-07 回写）

- **已实现**：`user_memory` 表（§3）与 `memory/service.py` 的 `load_memory`/`write_memory`/`extract_and_write`（覆盖语义同 §4）；目前唯一接线点是 `POST /plans` 在传 `organizer_user_id` 时 `load_memory` 注入长期偏好作缺省。
- **未激活**：读写链路以 `user_id`（users 表）为前提，依赖用户账号体系（PRD P2）；匿名 MVP 阶段前端不传 `organizer_user_id`，"会话开始注入 / 结束回写"（`extract_and_write` 无生产调用点）实际不触发。
- **现阶段承载**：匿名会话上下文由 `plans.conversation`（多轮消息兜底，主存 LangGraph checkpoint，DD-02/DD-15）+ `plans.constraints` 承载；长期记忆接线待账号体系落地后接通。

---

> 本模块让产品"越用越懂你"，但坚持两条：**覆盖语义**保证不被旧偏好误导，**隐私最小化**保证记忆不越界。会话内记忆已由 DD-02 checkpoint 覆盖，本模块专注跨会话长期偏好。
