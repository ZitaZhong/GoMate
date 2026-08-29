# DD-04 外部 Provider 抽象与 AI 能力层 · 详细设计

**详细设计系列 · 平台契约文档 · v1.0 · 2026 年 7 月**

> 本文定义两块平台能力：① **统一外部 Provider 抽象**（地图/天气/航班/搜索/LLM），内建"缓存→限流→重试→熔断→降级"五件套，并逐步 MCP 化；② **AI 能力层**（分层 LLM 路由、脱敏、BYO Key、PydanticAI 结构化抽取）。所有领域模块通过本层访问外部世界，**不得直接裸调外部 API**。
>
> **上游依据**：v1 §2.7/§4.3（Provider 抽象）、§7.1/§7.2（分层 LLM/脱敏）、v1.1 增补 D（MCP/PydanticAI/可观测）、DD-01 §9.1（Redis 缓存/限流/配额键）、DD-03（Fact/Evidence 抽取输出）。
> **下游消费者**：DD-05（embedding/rerank 也可视作 Provider）、DD-06/08/09/11/13 与所有需外部数据的节点。

---

## 1. 模块职责与边界

| 项 | 说明 |
|---|---|
| **职责** | 把所有外部依赖收敛为统一、可靠、可降级的调用层；把 LLM 使用收敛为分层路由 + 脱敏 + BYO Key + 结构化抽取。 |
| **边界内** | Provider 接口、五件套、各 Provider 规格、MCP 化、LLM 路由、`redact()` 脱敏、BYO Key、PydanticAI 抽取封装、配额治理、可观测。 |
| **边界外** | 检索算法（DD-05）、领域业务逻辑（各 DD）。本层只提供"稳定的原子能力"。 |
| **架构位置** | v1 §4.1"外部集成层 + AI 能力层"。 |

---

## 2. 设计目标与非目标

**目标**：① 任何外部单点失效不致空结果（降级到备用源/规则/明确 unknown）；② 成本可控（缓存 + 配额上限）；③ 模型中立（Qwen/DeepSeek/GLM 可换）+ BYO Key；④ 抽取输出即校验过的 `Fact/Evidence`（与 DD-03 严丝合缝）。

**非目标**：❌ 不在本层写业务；❌ 不让领域模块感知具体供应商（面向接口编程）。

---

## 3. 统一 Provider 抽象

```python
from typing import Protocol
from dataclasses import dataclass

@dataclass
class Req:
    op: str                 # 如 "geocode"/"route"/"poi_search"/"weather_hourly"
    params: dict
    cache_ttl: int          # 秒；见 §4.1 分层 TTL
    @property
    def key(self) -> str:   # 缓存键：cache:{provider}:{sha1(op+params)}
        ...

@dataclass
class Result:
    ok: bool
    data: dict | None
    source_type: str        # 用于 DD-03 定级：amap/qweather/variflight/...
    degraded: bool = False  # 是否走了降级路径

class Provider(Protocol):
    name: str
    async def call(self, req: Req) -> Result: ...
```

### 3.1 `ResilientProvider`（五件套）

```python
class ResilientProvider:
    """缓存优先 → 令牌桶限流 → 熔断 → 主调用 → 失败降级（备用源/规则兜底/明确 unknown）。"""
    def __init__(self, primary, fallbacks, cache, limiter, breaker): ...

    async def call(self, req: Req) -> Result:
        if hit := await self.cache.get(req.key):
            return Result(ok=True, data=hit, source_type=self.primary.name)
        if not self.limiter.allow(self.primary.name):        # 配额/QPS 保护
            return await self._fallback(req, reason="rate_limited")
        if self.breaker.is_open(self.primary.name):          # 熔断
            return await self._fallback(req, reason="circuit_open")
        try:
            r = await self.primary.call(req)
            await self.cache.set(req.key, r.data, ttl=req.cache_ttl)
            self.breaker.record_success(self.primary.name)
            return r
        except Exception as e:
            self.breaker.record_failure(self.primary.name)
            return await self._fallback(req, reason=str(e))

    async def _fallback(self, req, reason) -> Result:
        for fb in self.fallbacks:                            # 备用供应商 / 规则兜底
            try:
                r = await fb.call(req); r.degraded = True; return r
            except Exception:
                continue
        return Result(ok=False, data=None, source_type="unknown", degraded=True)  # 明确 unknown
```

> 对应 v1「韧性设计」表：任何失效最差返回 `unknown`（由 DD-03 标注为 `unknown` 态），不空结果。

---

## 4. 五件套细则

### 4.1 缓存（分层 TTL，Redis，DD-01 §9.1）

| Provider/op | TTL | 理由 |
|---|---|---|
| 地理编码 geocode | 30 天 | 地址→坐标几乎不变 |
| 路线/距离矩阵 route | 7 天 | 变化慢 |
| POI 搜索 | 3 天 | 变化较慢 |
| 天气 hourly/alert | 1 小时 | 时效强 |
| 航班时刻 | 12 小时 | 时刻变化慢（价格不缓存） |
| 搜索 API | 1 天 | 仅找入口 |
| LLM（相同输入） | 24 小时 | 省 token；抽取类可更长 |

### 4.2 限流 / 熔断 / 重试 / 降级

- **限流**：`rl:{provider}` 令牌桶（Lua 原子），全局 + 单域名双层。
- **配额**：`quota:{provider}:{yyyymmdd}` 日成本上限，接近阈值自动降级（换备用/规则）。
- **重试**：对幂等 GET 指数退避 2 次；对写/计费类不自动重试。
- **熔断**：滑动窗口失败率 > 阈值打开，半开探测恢复。
- **降级链**：主供应商 → 备用供应商 → 规则兜底 → `unknown` + 官方入口。

---

## 5. 各 Provider 规格（v0.1）

| Provider | op（示例） | 输入 → 输出 | 配额/计费 | 缓存 | 备用/降级 | source_type |
|---|---|---|---|---|---|---|
| **高德地图** | geocode / regeo / poi_search / route(walk/transit/drive) / distance_matrix | 地址/坐标/POI → 坐标/POI/耗时距离 | 免费日配额+超量按量 | 见 §4.1 | 百度/腾讯位置（同接口抽象）→ 直线估算+地图链接 | `amap` |
| **和风天气** | weather_hourly / minutely_precip / warning | 坐标+时段 → 降水概率/温度/预警 | 免费开发订阅+按量 | 1h | 彩云（降水专项）→ 无则标 unknown | `qweather` |
| **VariFlight** | flight_schedule / flight_status | 航段+日期 → 时刻/机型/准点率（**不取实时价**） | 商用付费 | 12h | 聚合数据 → 无则"机场对比+时段建议" | `variflight` |
| **搜索** | web_search | 查询 → 结果链接（**仅找官方入口**） | 按次 | 1d | 博查↔Tavily↔Serper 互备 | `search` |
| **LLM** | chat / extract / ocr / embed / rerank | 见 §6 | 按 token/图 | 24h（抽取类） | 见 §6 路由降级 | `llm` |

> **强约束**：VariFlight 只取**时刻/动态**，**不取实时票价**（价回官方/OTA）；搜索结果**仅用于找入口**，事实回官方核实（DD-03 闸一 `search`→未核实即 `unknown`）。

---

## 6. AI 能力层

### 6.1 分层 LLM 路由（v1 §7.1）

```python
LLM_ROUTES = {
    "constraint_parse": "qwen-plus",       # 中档：约束解析/追问
    "activity_extract": "qwen-turbo",      # 小模型：高频字段抽取
    "booking_ocr":      "qwen-vl-ocr",     # 多模态：截图/票据回填
    "research_reason":  "qwen-max",        # 高档：偏好冲突/取舍解释（低频）
    "search_entry":     "qwen-turbo",      # 找入口的抽取
}
# 模型中立：LangChain provider 抽象一行切换 Qwen/DeepSeek/GLM
def get_llm(task: str, byo_key: str | None = None):
    model = LLM_ROUTES[task]
    return make_client(model, api_key=byo_key or system_key(model))
```

**路由原则**：LLM/真实 API 为主路径——语义理解、抽取、推理类任务默认调模型，必须调的先小后大；规则/确定性服务仅作**显式标注的降级**（无 key/超时/熔断时启用，产出必须带 `degraded`/`estimated` 标注），禁止静默兜底产出低质结果。

### 6.2 脱敏中间件 `redact()`（v1 §7.2，进模型前强制）

```python
def redact(payload: dict) -> dict:
    """进 LLM 前：精确地址→商圈级；证件号/联系方式打码；个人预算→区间。"""
    p = deepcopy(payload)
    p = coarsen_locations(p)      # 门牌 → 商圈（对齐 DD-01 origin_area 粒度）
    p = mask_ids(p)               # 证件/手机号 → ****
    p = bandify_budget(p)         # 精确预算 → 区间
    return p
# 在 BFF↔Planner 边界与每次 LLM 调用前统一执行；模型侧永不见原始 PII。
```

### 6.3 BYO Key（数据主权，v1 §7.2）

- 用户可填自己的模型 Key（阿里云百炼/DeepSeek/OpenAI 兼容端点），请求**直连用户配额**；
- 服务端**不落 Key 明文**（KMS 加密或仅会话内存）；BYO 时不计入我方 `quota`。

### 6.4 PydanticAI 结构化抽取（与 DD-03 接线）

```python
from pydantic_ai import Agent
# 抽取子调用输出即校验过的 Fact；再经 DD-03 闸二/闸一定级
async def extract_fact(task: str, text_or_image, schema, source_type: str) -> list[Fact]:
    agent = Agent(get_llm(task), output_type=schema)   # 结构化、类型安全
    out = await agent.run(text_or_image)
    facts = to_facts(out, source_type=source_type)      # 包 Fact
    return [enforce_provenance_by_field(f) for f in map(validate_fact, facts)]  # DD-03
```

> 预算小则用纯 Pydantic + Function Calling（v1.1 D），不强依赖 PydanticAI；抽取 schema 见 DD-06（活动）与 DD-10（回填）。

---

## 7. MCP 化路径（v1.1 D）

- **v0.1**：Provider 用普通类封装（如上），**避免过度工程**。
- **演进**：工具增多后，把地图/天气/航班/搜索封装为 **MCP Server**，`ResilientProvider.primary` 内部实现改为 **MCP client**——对领域模块**接口不变**。对齐 PRD v0.3"开放数据源插件机制"，浏览器扩展（DD-14）亦可复用同一套 MCP 工具定义。

---

## 8. 配额治理与成本（v1 §11）

- 每 Provider 一个令牌桶 + 日成本上限；接近上限自动降级并告警。
- 缓存命中率、各 Provider 日调用量/花费进埋点，持续优化 TTL 与命中。
- BYO Key 分流：重度用户走自带 Key，降低我方成本。

---

## 9. 可观测（v1.1 D）

- 每次外部调用/LLM 调用一个 OTel span（LangSmith 或 Langfuse）：记 op、耗时、命中/降级、token/成本。
- 降级率、熔断次数、配额触顶次数作为健康指标。

---

## 10. 效果与验收标准（DoD）

1. **降级链**：mock 高德超时 → 自动走备用/直线估算，返回 `degraded=True` 且 `source_type` 正确（供 DD-03 定级）。
2. **缓存**：相同 `Req` 二次调用命中缓存（Redis 有键、无外呼）。
3. **限流/配额**：压测触发令牌桶/日上限 → 自动降级不报错。
4. **脱敏**：给定含门牌/证件的 payload，`redact()` 后无原始 PII（单测断言）。
5. **抽取**：截图/文本经 `extract_fact` 产出校验过的 `Fact`，`llm` 来源字段被 DD-03 正确降级。
6. **BYO Key**：填入用户 Key 时走用户配额、不落明文（审计）。

---

## 11. 开发任务拆解

1. `Provider` 接口 + `ResilientProvider` 五件套（1.5d）
2. 高德 Provider（geocode/route/poi/matrix）+ 缓存（1.5d）
3. 和风天气 + VariFlight + 搜索 Provider（1.5d）
4. LLM 路由 + 模型中立客户端 + BYO Key（1d）
5. `redact()` 脱敏中间件 + 单测（0.5d）
6. PydanticAI 抽取封装（接 DD-03）（1d）
7. 配额/可观测接线（1d）

## 12. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 外部 API 成本超预期 | 缓存分层 + 日上限 + 探索期粗估 + BYO Key 分流 |
| 供应商接口变更 | Provider 接口隔离 + 备用供应商 + 契约测试 |
| 脱敏遗漏导致 PII 进模型 | 边界统一 `redact()` + 单测覆盖字段 + 抽样审计 |
| MCP 过早引入增加复杂度 | v0.1 普通封装，工具增多再 MCP（接口不变） |

---

> 本层为所有外部访问与 LLM 调用的唯一入口。领域模块不得裸调外部 API 或直接实例化 LLM 客户端。

---

## 13. v2 增补：web 深搜 Provider + 记忆/流式（对齐 DD-15/16/17）

- **新增 web 深搜 Provider**：`op="web_search_deep"`（**默认博查 Bocha**——面向 AI 的中文搜索，Web Search + Semantic Reranker，最贴合国内；可配 **Tavily / Exa（语义检索强）/ Serper**，由 `.env` `DEEP_RESEARCH_SEARCH_PROVIDER` 选择；**并发 + 流式**，`source_type=search`）；缓存 1d；多家互备降级；**仅用于找官方入口**（DD-17）。
- **新增记忆支撑**：`embed` 复用于 `user_memory` 向量；可选直接用 Mem0 库（pgvector 后端，schema 对齐 DD-16 §3），均走本层。
- **LLM 路由增补**：`intent_classify=qwen-turbo`（DD-15 意图分类）、`research_brief=qwen-plus`（DD-17 Scope 生成 brief）、`research_supervisor=qwen-plus`（DD-17 反思-补缺/gap 分析）、`research_extract=qwen-turbo`（DD-17 子研究抽取）、`memory_extract=qwen-turbo`（DD-16 抽取）。
- **流式**：web 深搜与 chat 支持 streaming，供 DD-15 对话流与 DD-17 进度事件。
- **配额/熔断**：深搜走令牌桶 + 日成本上限（§8）；深搜属高成本，需严控并发与触发频次（DD-17 §9）。
