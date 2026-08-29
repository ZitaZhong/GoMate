# RAG 与记忆框架深度洞察与技术选型

**版本 v1.0 · 面向「周末去哪儿」的检索与记忆技术白皮书 · 2026 年 7 月**

> 本文承接 `01_竞品技术方案深度分析报告.md`、`02_开源项目技术方案深度分析报告.md` 与 `技术方案/周末去哪儿_技术架构与实现方案_v1.md`。
> 目标有两个：
> 1. **深度洞察**——以架构师视角，基于**真实源码**（而非营销文档）剖析业界主流 RAG 与记忆框架的架构、设计、代码实现、优劣与适用边界；
> 2. **技术选型**——在此基础上，给出契合本产品三条硬约束（**不做交易 / 证据优先 / 轻后端重证据**）的检索与记忆技术方案。
>
> 说明：文中类名、函数名、算法步骤、评测数字均取自各项目 GitHub 主分支源码与官方论文/博客（截至 2026-07），跨厂商基准（LoCoMo / LongMemEval / DMR）存在口径争议，已在相应位置标注，选型应以自有数据复测为准。

---

## 目录

1. [先把技术锚定到产品：WhereToGo 为什么需要 RAG 与记忆](#1-先把技术锚定到产品wheretogo-为什么需要-rag-与记忆)
2. [RAG 工程框架深度剖析（代码级）](#2-rag-工程框架深度剖析代码级)
3. [高级与图 RAG 范式：从向量到图、从检索到推理](#3-高级与图-rag-范式从向量到图从检索到推理)
4. [向量与检索基础设施：索引算法、向量库、混合检索与重排](#4-向量与检索基础设施索引算法向量库混合检索与重排)
5. [AI 记忆框架深度剖析：读写演化遗忘](#5-ai-记忆框架深度剖析读写演化遗忘)
6. [横向洞察：业界正在收敛的共性模式与关键权衡](#6-横向洞察业界正在收敛的共性模式与关键权衡)
7. [面向「周末去哪儿」的技术选型（落地方案）](#7-面向周末去哪儿的技术选型落地方案)
8. [附录：核心信源与源码索引](#8-附录核心信源与源码索引)

---

## 0. 三分钟速览（架构师核心结论）

1. **RAG 与记忆是两件事，本产品两者都要，但用法不同。**
   - **RAG（只读检索）** 服务于产品的**证据体系**：活动库、城市档案、餐饮 POI、攻略/官方页——把"带来源的事实"精准喂给 LLM。
   - **记忆（读写 + 演化 + 遗忘）** 服务于产品的**个性化与协作**：用户长期偏好、单次规划的多轮会话状态、跨会话的偏好漂移。
   - 一句话：**RAG 是"读"，Memory 是"读 + 写决策 + 冲突消解 + 时间性 + 遗忘"**。记忆框架的检索层复用 RAG 技术。

2. **业界检索管线已收敛为事实标准**：`稠密向量召回 + 稀疏/BM25 召回 → RRF 融合 → cross-encoder 重排 → top-k 喂 LLM`。R2R、FastGPT、Haystack、LlamaIndex、Milvus、Qdrant、Weaviate 的实现思路高度一致，差异只在"在哪一层做融合"。

3. **图 RAG（GraphRAG/LightRAG/HippoRAG）解决的是"全局理解 / 多跳推理"，不是本产品的主要矛盾。** 本产品的检索需求以**事实精确检索 + 地理/时间强过滤**为主（"本周末 + 北京 + 3km 内 + 展览"），这恰恰是**向量 + BM25 + 结构化过滤**的主场，图方法在此是过度设计。

4. **不要为了 RAG/记忆引入新的基础设施。** 本产品已定 `PostgreSQL + PostGIS + pgvector + Redis + LangGraph`。**pgvector 足以承载活动库与用户记忆到数千万级**；记忆层用 **Mem0 思路的语义偏好记忆 + LangGraph 原生会话状态 + 轻量自研偏好覆盖**即可，无需引入 Neo4j / Milvus / 专用记忆服务。**每多一个中间件，就多一份运维与一致性风险，这与"轻后端"硬约束冲突。**

5. **证据优先是本产品的护城河，也是选型的第一过滤器。** 任何"黑盒生成、不可溯源"的检索/记忆方案（如把 LLM 生成物直接当事实存入记忆）都与 Provenance Guard 冲突，一票否决。**能溯源、能标注 verification_status、能表达"不知道"的方案才入选。**

---

## 1. 先把技术锚定到产品：WhereToGo 为什么需要 RAG 与记忆

在评价任何框架之前，先明确本产品到底在哪些环节需要"检索"与"记忆"。脱离场景谈选型是耍流氓。

### 1.1 产品中的 RAG（检索）触点

| 产品环节（PRD/技术方案出处） | 检索什么 | 检索特征 | 对可信度的要求 |
|---|---|---|---|
| **当周活动调研**（PRD 5.3 / 技术方案 §8 情报流水线） | 活动库（带证据的结构化记录） | 城市 + 时间窗 + 品类 + 地理，**强过滤 + 语义** | 极高：`verification_status` 决定能否作核心活动 |
| **目的地发现**（PRD 5.2） | 城市档案 city_playbook + 活动聚合 | 结构化为主 + 少量语义 | 高 |
| **餐饮/POI 推荐**（PRD 5.7 / §2.6） | 高德 POI + 用户 BYO 链接 | 地理动线 + 品类 + 语义 | 中：营业时间/电话需标注来源 |
| **回填抽取的检索校验**（PRD 5.4E/5.5D） | 官方公开页正文 | 单页正文抽取 | 高：抽取须带 evidence_quote |
| **攻略/解释性问答**（潜在） | 城市档案 + 活动 + 常识 | 语义为主 | 高：不许编造交通/票价 |

**关键洞察**：本产品的检索**不是**"海量非结构化文档问答"（那是企业知识库 RAG 的主场），而是**"结构化 + 半结构化 + 地理时空"的精准召回**。活动记录已经是结构化的 `activity` 表（见技术方案 §6.3 DDL），带 `location GEOGRAPHY`、`start_at`、`category`、`embedding VECTOR(1024)`。因此本产品的"RAG"更接近**带语义增强的多条件检索**，而非经典的"切块-嵌入-召回-拼接"。

### 1.2 产品中的记忆触点

| 产品环节 | 记忆类型 | 生命周期 | 出处 |
|---|---|---|---|
| **单次规划的多轮状态**（本次预算/人数/已否决城市/待确认清单） | 工作记忆（会话内） | 一次规划（可跨天，靠 interrupt 挂起） | 技术方案 §5 状态机 `TripPlanState` |
| **用户长期偏好**（口味、出行半径、忌讳、是否接受夜车/飞机） | 语义记忆（长期） | 跨会话，随时间缓慢漂移 | PRD 10 路线图 v0.2「用户历史偏好」 |
| **多人聚合的成员约束** | 会话内 + 隐私脱敏 | 一次协作 | PRD 5.1 / 技术方案 §6.4 |
| **用户收藏链接池** | 情景/资产记忆 | 长期 | PRD 10 v0.2 |

**关键洞察**：本产品的"多轮会话状态"**已经由 LangGraph 的 `Checkpointer(Postgres)` 承载**（技术方案 §5.2）——这本质上就是一套"会话工作记忆"。因此真正需要**新增**的记忆能力，只有**跨会话的用户长期偏好**这一块。这个判断直接决定了选型的克制程度：**不要用一个重型记忆框架去重复 LangGraph 已经做好的事**。

### 1.3 选型的三个硬过滤器（源自 PRD/技术方案硬约束）

1. **证据优先过滤器**：方案必须能**溯源**（保留 source_url / fetched_at）、能**分级**（六种 `verification_status`）、能**表达不知道**（unknown）。→ 排除"LLM 生成即事实"的记忆写入模式在事实类字段上的应用。
2. **轻后端过滤器**：方案应**复用现有 PG/Redis/LangGraph**，避免引入 Neo4j、Milvus、独立记忆服务等新中间件，除非有不可替代的收益。→ 提高任何"必须配套图数据库/专用向量库"方案的准入门槛。
3. **数据主权与隐私过滤器**：支持 BYO Key、脱敏边界（§7.2）、多人聚合不泄露个体（§6.4）。→ 记忆存储必须支持作用域隔离（user/session）与字段级脱敏。

带着这三个过滤器，我们再去看业界的框架，标准就非常清晰了。

---

## 2. RAG 工程框架深度剖析（代码级）

本节剖析六个主流"框架/平台"：**LlamaIndex、Haystack、RAGFlow、R2R、Dify、FastGPT**。前三四个是"框架/引擎"（代码级组装），后两个偏"平台"（配置级交付）。

### 2.1 LlamaIndex —— "一切皆 Node + Transformation" 的数据框架

- 仓库：[run-llama/llama_index](https://github.com/run-llama/llama_index)，核心包 `llama-index-core`

**架构核心抽象**：LlamaIndex 的设计哲学是把文档处理抽象成统一的"节点变换"。

- `Document` / `BaseNode`（`core/schema.py`）：数据原子单位。`Document` 是 `BaseNode` 子类——**文档与分块在管道里同构处理**。
- `TransformComponent`：统一可调用转换接口（`__call__` / `acall`）。**`SentenceSplitter`（分块器）和 `Embedding` 模型都实现它**——分块与向量化在类型上等价。
- 查询侧四层：`Index`（`BaseIndex`）→ `as_retriever()` → `Retriever` → `QueryEngine`。**`QueryEngine = Retriever + ResponseSynthesizer`**。

**代码级洞察：`IngestionPipeline`（写入侧编排器，`core/ingestion/pipeline.py`）**

默认 transformations 就是"分块 + 嵌入"：

```python
def _get_default_transformations(self) -> List[TransformComponent]:
    return [SentenceSplitter(), Settings.embed_model]
```

它用**内容哈希做缓存**，避免重复分块/嵌入（对增量更新是关键）：

```python
def get_transformation_hash(nodes, transformation) -> str:
    nodes_str = "".join([n.get_content(metadata_mode=MetadataMode.ALL) for n in nodes])
    transform_string = remove_unstable_values(str(transformation.to_dict()))
    return sha256((nodes_str + transform_string).encode("utf-8")).hexdigest()
```

增量更新的核心是 `DocstoreStrategy`：`UPSERTS`（按 id 判断，hash 变了则删旧向量再写新）、`DUPLICATES_ONLY`、`UPSERTS_AND_DELETE`。`_handle_upserts()` 里若 `existing_hash != node.hash` 则先 `vector_store.delete(ref_doc_id)` 再重写——**这就是"重跑管道只处理变更文档"的工程要点**。

**检索能力**：分块有 `SentenceSplitter` / `SemanticSplitterNodeParser`（按 embedding 相似度断句）/ `HierarchicalNodeParser`（父子块，配 `AutoMergingRetriever`）；索引有 `VectorStoreIndex` / `SummaryIndex` / `PropertyGraphIndex`（GraphRAG）/ `DocumentSummaryIndex`；query 变换有 `HyDEQueryTransform` / `SubQuestionQueryEngine`（问题拆解）；rerank 走 `node_postprocessors`（`SentenceTransformerRerank` / `LLMRerank` / `CohereRerank`）；多路融合走 `QueryFusionRetriever`（RRF）。

**优劣 / 场景 / 二次开发**：抽象最完整、集成生态最庞大（数百 reader/vector store）；代价是抽象层多、API 漂移快、`llama-index-*` 命名空间包依赖略复杂，生产部署需自建服务层。**适合算法/研发团队快速搭复杂 RAG（父子块/GraphRAG/agent）**。二次开发难度**最低**——继承 `TransformComponent` / `BaseRetriever` / `BaseNodePostprocessor` 即可插入自定义逻辑，是所有框架里扩展点最规范的。

### 2.2 Haystack 2.x —— 显式 DAG + 类型化端口的生产 Pipeline

- 仓库：[deepset-ai/haystack](https://github.com/deepset-ai/haystack)（2.x）

**架构核心抽象**：`Component`（`@component` 装饰器，声明 `@component.output_types` 与 InputSocket/OutputSocket 类型）+ `Pipeline`（`add_component` + `connect("retriever", "prompt_builder.documents")` 构建 DAG，端口按名称+类型校验）+ `DocumentStore` 与 `Retriever` 解耦。

**代码级洞察：`Pipeline.run` 不是简单拓扑排序，而是优先级队列调度器**（`core/pipeline/pipeline.py`），以支持带环（cycles）与分支的图：

```python
ordered_component_names = sorted(self.graph.nodes.keys())  # 确定性，与插入顺序无关
component_visits = dict.fromkeys(ordered_component_names, 0)
priority_queue = self._fill_queue(ordered_component_names, inputs, component_visits)
while True:
    candidate = self._get_next_runnable_component(priority_queue, component_visits)
    if candidate is None: break
    priority, component_name, component = candidate
    if priority == ComponentPriority.BLOCKED: ...  # 检测死锁并告警
```

`ComponentPriority` 有 `HIGHEST / READY / DEFER / BLOCKED` 四级，`DEFER`（等待更多输入）通过缓存的拓扑排序打破僵局。**生产级特性（源码级证据）**：
- **断点/快照**：`break_point: Breakpoint` + `PipelineSnapshot`，出错时序列化全部 `inputs`/`component_visits`/`pipeline_outputs` 到 JSON，可从中断处 `resume`——**把 pipeline 当"可恢复工作流"运行**。
- **异步并发**：`run_async_generator(concurrency_limit=4)` 用 `asyncio.Semaphore` 并行调度 READY 组件，边完成边 yield。
- 单组件执行强制契约：组件必须返回 `Mapping`，否则抛 `PipelineRuntimeError`。

**优劣 / 场景**：工程成熟度最高之一，显式 DAG + 类型校验 + 断点恢复 + 序列化（YAML）非常适合生产；代价是抽象比 LlamaIndex 略"重"、集成数量少于 LlamaIndex。**适合需要可维护、可部署、可观测的生产 LLM 应用的工程团队**。

> **与本产品的关系**：Haystack 的"可恢复流水线 + 类型化 DAG"理念很好，但本产品**已用 LangGraph 承担编排与 checkpoint**，两者定位重叠。Haystack 的价值更多在于**理念印证**（我们的方向是对的），而非引入。

### 2.3 RAGFlow —— 深度文档理解，护城河是 DeepDoc

- 仓库：[infiniflow/ragflow](https://github.com/infiniflow/ragflow)

**差异化**：`DeepDoc`（视觉文档解析）+ 模板化 chunk + Infinity/ES 混合索引。

**代码级洞察：DeepDoc 不是简单抽字，而是视觉模型驱动的版面还原**（`deepdoc/parser/pdf_parser.py`，`RAGFlowPdfParser`，2000+ 行）：

```python
class RAGFlowPdfParser:
    def __init__(self, **kwargs):
        self.ocr = OCR()
        self.layouter = LayoutRecognizer(recognizer_domain)  # 版面识别(ONNX/Ascend)
        self.tbl_det = TableStructureRecognizer()            # 表格结构识别(TSR)
        self.updown_cnt_mdl = xgb.Booster()                  # XGBoost 判断文本块是否拼接
        self.updown_cnt_mdl.load_model(".../updown_concat_xgb.model")
```

`__ocr()` → `_layouts_rec()`（给每个框打 title/text/table/figure 类型）→ `_table_transformer_job()`（表格还原为 HTML）→ `_concat_downward()`（**用 XGBoost + 几何特征判断跨行/跨栏/跨页文本是否属同一语义块**）。这是 RAGFlow "解决 Garbage-In" 的核心，远超普通字符流抽取。分块是 **token 级两段式**（先按多字符正则 `\n!?。；！？` 切细，再 `naive_merge` 合并到 `chunk_token_num` 默认 512），支持父子块与 `rag/app/` 下 naive/qa/paper/laws/manual/table 等多套模板。检索默认走 **Infinity**（自研向量+全文+张量库）或 ES，**原生 hybrid**。

**优劣 / 场景**：**文档理解质量业界最强之一**；代价是 DeepDoc 依赖 GPU/大内存（875 页 PDF 曾 OOM 32GB）、部署重。**适合以扫描件/复杂 PDF/表格为主的企业知识库（金融、法律、政务）**。

> **与本产品的关系**：本产品的数据来源是**官方网页与结构化活动**，不是复杂 PDF/扫描件，DeepDoc 的重型视觉解析**用不上**。但其"网页正文清洗 → 干净 Markdown → LLM 抽取"的思路，本产品在情报流水线（§8）里已用 Jina Reader/Firecrawl 覆盖。

### 2.4 R2R —— 单一 Postgres 后端的一体化 RAG（与本产品最同构）

- 仓库：[SciPhi-AI/R2R](https://github.com/SciPhi-AI/R2R)

**架构核心**：围绕 RESTful API + **单一 Postgres(pgvector)** 后端。`PostgresDatabaseProvider` 聚合一组 Handler：

```python
class PostgresDatabaseProvider(DatabaseProvider):
    self.chunks_handler        = PostgresChunksHandler(...)   # 向量+全文
    self.entities_handler      = PostgresEntitiesHandler(...) # ↓ GraphRAG
    self.relationships_handler = PostgresRelationshipsHandler(...)
    self.communities_handler   = PostgresCommunitiesHandler(...)
async def initialize(self):
    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")   # pgvector
    await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")  # 三元组模糊
```

**设计洞察**：向量、全文、文档、知识图谱（entities/relationships/communities）**全部落在同一个 Postgres schema**，无需额外向量库——**这正是本产品"轻后端"要的形态**。

**代码级洞察：Hybrid Search 的加权 RRF**（`chunks.py` 的 `PostgresChunksHandler.hybrid_search`）——语义用 pgvector `<=>` 距离算子，全文用 Postgres 原生 FTS（`websearch_to_tsquery` + `ts_rank`），融合用加权 RRF：

```python
for hyb in combined_results.values():
    semantic_score  = 1 / (rrf_k + hyb["semantic_rank"])
    full_text_score = 1 / (rrf_k + hyb["full_text_rank"])
    hyb["rrf_score"] = (semantic_score * semantic_weight
                        + full_text_score * full_text_weight) / (semantic_weight + full_text_weight)
sorted_results = sorted(combined_results.values(), key=lambda x: x["rrf_score"], reverse=True)
```

工程细节：融合前对候选做双倍窗口裁剪（`rank <= limit*2`）控爆炸；把 `semantic_rank`/`full_text_rank` 写回 `metadata` 便于可解释调试。

**优劣 / 场景**：**单一 Postgres = 极简生产运维**；REST + 权限 + 多租户 + hybrid + graph + agent 一体。代价是强绑 Postgres、社区规模小于 LlamaIndex/Haystack。**适合想自托管、一体化、少运维的生产 RAG 服务**。

> **与本产品的关系**：R2R 是本节里**与本产品架构最同构**的项目——"单 Postgres 承载向量+全文+图谱 + 加权 RRF 混合检索"几乎就是本产品应有的检索层设计。**其 `hybrid_search` 的 SQL 与加权 RRF 实现，可直接作为本产品活动检索的参考蓝本**。

### 2.5 Dify / FastGPT —— LLMOps 平台内置 RAG

**Dify**（[langgenius/dify](https://github.com/langgenius/dify)，Python）：知识库 → `index_processor` → `RetrievalService`。代码级洞察是**多路并行检索**（`ThreadPoolExecutor` 并发多 query/多附件），三种底层方法解耦（`keyword_search` / `embedding_search` / `full_text_index_search`），Hybrid 两种融合：`reranking_model`（重排模型）或 `weighted_score`（加权分数，无需 rerank 模型）。`Vector`/`Keyword` 工厂屏蔽 20+ 向量库差异。

**FastGPT**（[labring/FastGPT](https://github.com/labring/FastGPT)，TypeScript）：`searchDatasetData` → `multiQueryRecall` → 加权 RRF → rerank → 过滤。代码级洞察是**图文混合多路召回 + 加权 RRF**（`concatWeightedRecallLists`），文本侧 = embedding ⊕ fullText 按 `embeddingWeight` 加权 RRF，再 rerank，最后与图片侧融合；过滤顺序固定为**去重 → 相似度阈值 → token 上限**。

**优劣 / 场景**：两者都是"配置级交付"的平台，UI 完善、上手快，RAG 是其一环；深度定制受平台约束。**适合业务团队快速上线企业 AI 应用**（Dify 生态最全，FastGPT TS 栈轻量）。

> **与本产品的关系**：本产品需要**代码级可控的证据体系与 Provenance Guard**，平台的"黑盒配置"无法表达"六种 verification_status + 字段级来源白名单"，因此**不适合作为核心**（这与技术方案 §3.1 对 Dify/Coze 的判断一致）。但其"多路并行召回 + 加权 RRF + 固定过滤顺序"的**检索链路设计**值得借鉴。

### 2.6 RAG 框架横向对比

| 维度 | LlamaIndex | Haystack 2.x | RAGFlow | R2R | Dify | FastGPT |
|---|---|---|---|---|---|---|
| 语言 | Python | Python | Python | Python | Python | TypeScript |
| 定位 | 数据/索引框架 | 生产 Pipeline 编排 | 深度文档理解引擎 | 一体化 RESTful RAG | LLMOps 平台 | LLMOps 平台 |
| 核心抽象 | Node/Transform/Index | Component + DAG(socket) | DeepDoc + chunk 模板 | Postgres Provider/Handler | Dataset + RetrievalService | 工作流 + multiQueryRecall |
| 检索/融合 | dense 为主，FusionRetriever RRF | BM25/embedding，Joiner RRF | 原生 hybrid | **原生 hybrid + 加权 RRF** | 语义/关键词/全文/hybrid | **加权 RRF** |
| 后端依赖 | 任意向量库 | 任意 DocumentStore | Infinity/ES(重) | **仅 Postgres** | 20+ 向量库 | PG+Mongo |
| 生产特性 | 需自建服务层 | **断点/快照/序列化/async** | 可视化产品 | REST/权限/多租户 | 完整平台 | 完整平台 |
| 二次开发 | **最规范** | 低-中(@component) | 中(难改视觉) | 中(SQL/async) | 中(平台约束) | 中(TS 模块化) |
| 对本产品的价值 | 借鉴增量 upsert 与 node 抽象 | 印证"可恢复流水线"方向 | 情报流水线正文抽取思路 | **检索层蓝本（最同构）** | 借鉴多路并行召回 | 借鉴加权 RRF 与过滤顺序 |

---

## 3. 高级与图 RAG 范式：从向量到图、从检索到推理

上一节是"框架"，本节是"方法论/算法范式"——它们决定了**索引与检索的智能上限**。这些技术可叠加在任意框架之上。

### 3.1 Microsoft GraphRAG —— 全局理解之王，但索引最贵

- 仓库：[microsoft/graphrag](https://github.com/microsoft/graphrag) · 论文 arXiv:2404.16130

**解决的核心问题**：向量 RAG 无法回答"全局性/主题聚合型"问题（如"整个语料库的主要主题是什么"）。这类 Query-Focused Summarization (QFS) 需要对整个数据集的全局理解，而 top-k 向量检索只能命中局部片段。

**思路**：LLM 把非结构化文本抽成知识图谱 → 图社区检测切分层次社区 → 预先为每个社区生成"社区报告"摘要 → 查询时聚合社区报告。**索引 pipeline（真实工作流）**：
1. `create_base_text_units`：切 chunk（默认 1200 token / overlap 100）。
2. `extract_graph`：**多轮 gleaning**——prompt 让 LLM 抽实体 `(name, type, description)` 与关系 `(source, target, description, strength)`，再追加"是否还有遗漏"continue prompt 重复 `max_gleanings` 轮。**这是索引 token 成本高的主因——每 chunk 要 1+N 次 LLM 调用**。
3. `create_communities`：同名实体描述做 LLM 摘要合并，再用 **Leiden 算法**（`graspologic.hierarchical_leiden`）做层次化社区划分（保证社区内连通性）。
4. `create_community_reports`：**自底向上**为每个社区生成结构化报告（title/summary/rating/findings），上层社区 token 超限则用子社区报告递归摘要。

**检索四模式**（`query/structured_search/`）：**Local Search**（面向具体实体：向量命中实体 → 图扩展 → 多源上下文）、**Global Search**（面向全局主题：**map-reduce**，Map 把各社区报告分批送 LLM 产出带评分的要点，Reduce 汇总生成答案，是最贵的模式）、**DRIFT Search**（local 增强）、**Basic Search**（朴素向量兜底）。

**成本/效果**：README 顶部即 ⚠️ 警告 "indexing can be an expensive operation"；论文报告在综合性/多样性上 Global GraphRAG 对朴素 RAG 胜率常 70-80%+。**增量能力弱**——新文档需近乎重算社区结构，这正是 LightRAG/HippoRAG 主打的反差点。

### 3.2 LightRAG —— GraphRAG 的轻量平替，增量友好

- 仓库：[HKUDS/LightRAG](https://github.com/HKUDS/LightRAG)（EMNLP 2025）· `pip install lightrag-hku`

**定位**（README 原文）："an efficient alternative to Microsoft GraphRAG"。去掉昂贵的社区报告与多轮摘要，用**图索引 + 双层检索（dual-level retrieval）**达到相近效果。

**双层检索**：查询时 LLM 抽两类关键词——**Low-level（局部）**匹配 KG 实体节点（回答具体对象/事实），**High-level（全局）**匹配 KG 关系边（捕获跨文档宏观主题）。5 种 mode：`local` / `global` / `hybrid` / `naive`（纯向量）/ `mix`（默认，local+global+naive 融合，配 reranker 最佳）。

**增量更新（最大卖点）**：新数据只需生成局部图，再通过**集合合并（set merging）**并入现有图，**无需重建全局索引**；删除时复用构建期 LLM 缓存。这是 GraphRAG 做不到的。四类存储 `KV / VECTOR / GRAPH / DOC_STATUS`，生产可用 PostgreSQL 一体化。论文对照：效果与 GraphRAG 同档，成本显著更低。

### 3.3 HippoRAG 2 —— 单步多跳，索引最省

- 仓库：[OSU-NLP-Group/HippoRAG](https://github.com/OSU-NLP-Group/HippoRAG) · 论文 arXiv:2502.14802 *From RAG to Memory*（ICML'25）

**神经生物学启发**（海马体索引理论）：新皮层=passage，海马体索引=知识图谱，模式补全=**Personalized PageRank (PPR)**。**解决多跳推理**：标准 RAG 要么迭代多次检索（慢贵），要么召回不全；HippoRAG 用**单步 PPR 传播**一次完成跨文档知识整合。

**算法**：离线用 OpenIE 抽开放三元组建 schema-less KG + 同义词边；在线时 LLM 从 query 抽实体 → 匹配 KG 查询节点 → 计算 node specificity（类比 IDF）作初始权重 → 跑一次 PPR 传播（**一次传播模拟多跳**）→ 映射回 passage 打分取 top-k。HippoRAG 2 进一步**统一稠密与稀疏检索**（passage 节点也纳入图），在事实/sense-making/多跳三维度全面超越。**索引资源消耗远低于 GraphRAG/RAPTOR/LightRAG**，比迭代检索（IRCoT）快 6-13 倍、便宜 10-30 倍。

### 3.4 RAPTOR —— 递归聚类摘要树

- 仓库：[parthsarthi03/raptor](https://github.com/parthsarthi03/raptor)（ICLR 2024）

**思路**：传统 RAG 只检索短连续 chunk，无法整合长文档多个部分。RAPTOR **递归 embed → cluster → summarize** 自底向上建多层摘要树：叶子是原始 chunk，越往上摘要越抽象。**建树**：chunk embedding → **UMAP 降维 + GMM 软聚类**（一个 chunk 可属多簇，BIC 定簇数）→ 每簇 LLM 摘要成父节点 → 递归。**检索**推荐 **Collapsed Tree**（把所有层级节点拉平，统一按相似度取 top-k）。论文在 QuALITY 上 +20% 绝对准确率。**增量差**（新文档需重建子树），**适合长文档（书/论文/财报）跨章节整合**。

### 3.5 Anthropic Contextual Retrieval —— 改造成本最低、见效最快

- 出处：[Anthropic 工程博客](https://www.anthropic.com/engineering/contextual-retrieval)（2024.09）

**思路**：切 chunk 破坏上下文（"The company's revenue grew 3%"丢失了"哪家公司/哪季度"）。方案：**在 embed 和建 BM25 索引前，用 LLM 给每个 chunk 前置 50-100 token 的上下文说明**（Contextual Embeddings + Contextual BM25）。成本靠 **prompt caching**（整篇文档缓存一次，各 chunk 复用）压到约 $1.02/百万文档 token。

**真实效果数字**（用 `1 − recall@20` 衡量，越低越好）：
- Contextual Embeddings：召回失败率 **-35%**
- + Contextual BM25：**-49%**
- + Reranking（先取 top-150 再精排 top-20）：**-67%**

**这是任何已有向量 RAG 的最高性价比升级**——不改架构，只加预处理。

### 3.6 检索控制流：Self-RAG / CRAG / Adaptive / Agentic

这四者不是索引方法，而是**"何时检索、如何纠错、如何路由、如何编排"**的决策框架，可正交叠加在任意索引方法上。

- **Self-RAG**（arXiv:2310.11511）：训练 LM 用**反思令牌（reflection tokens）**控制——`[Retrieve]`/`[No Retrieval]`（按需检索）、`[IsRel]`（相关性）、`[IsSup]`（证据支撑度）、`[IsUse]`（有用性）。**"按需检索 + 自评证据支撑"的思想对本产品的防幻觉极有价值**。
- **CRAG（Corrective RAG）**（arXiv:2401.15884）：轻量**检索评估器**给检索质量打分 → Correct（切 knowledge strips 过滤）/ Incorrect（触发 web search 兜底）/ Ambiguous（结合）。plug-and-play，常用 LangGraph 实现状态机。
- **Adaptive RAG**（arXiv:2403.14403）：训 query 复杂度分类器，简单→不检索、单跳→单步检索、多跳→迭代检索，**用最小代价匹配问题难度**。
- **Agentic RAG**：把检索包装成 agent 工具，ReAct/plan-execute 循环编排多源检索——最通用最灵活但最贵最慢。

### 3.7 混合检索与重排（工程基座）

- **ColBERT / Late Interaction**：passage 编码成 **token 级 embedding 矩阵**，检索用 **MaxSim**（query 每 token 取与 passage 所有 token 的最大相似度再求和）。精度高于单向量，可作端到端检索器或重排器（RAGatouille 便捷入口）。
- **BGE Reranker**：**cross-encoder**（query+doc 拼接过 BERT 输出单一分数），精度高但慢，用于精排 top-N。`bge-reranker-v2-m3` 多语言、中文强。
- **RRF（Reciprocal Rank Fusion）**：`RRF(d) = Σ 1/(k + rank_i(d))`，k 常取 60。**只用排名不用原始分数**，天然免去不同检索器分数不可比问题，是**混合检索的默认融合方法**。

### 3.8 高级 RAG 横向对比与按查询类型选型

| 技术 | 索引结构 | 索引成本 | 增量更新 | 最擅长的查询 |
|---|---|---|---|---|
| **GraphRAG** | KG + Leiden 社区 + 社区报告 | 🔴 极高 | 🔴 弱 | 全局理解 / QFS / sensemaking |
| **LightRAG** | KG + 向量（双层） | 🟡 中 | 🟢 强 | 全局+细节兼顾，成本敏感 |
| **HippoRAG 2** | OpenIE KG + PPR | 🟢 最低（图方案中） | 🟢 好 | 多跳推理 / 联想 / 记忆 |
| **RAPTOR** | 递归聚类摘要树 | 🟡 中 | 🔴 弱 | 长文档跨章节整合 |
| **Contextual Retrieval** | 上下文增强 chunk + 向量+BM25 | 🟢 低（靠缓存） | 🟢 强 | **通用事实检索（召回失败-49%）** |
| **向量+BM25+rerank** | 向量 + 倒排 | 🟢 低 | 🟢 强 | **事实精确检索 + 强过滤** |

| 查询类型 | 首选 | 说明 |
|---|---|---|
| 全局理解 / 主题聚合 | GraphRAG(Global) / RAPTOR | 向量 RAG 无能为力 |
| 多跳推理 / 跨文档联想 | HippoRAG 2 | PPR 单步多跳最省 |
| **事实性精确检索（本产品主场）** | **向量+BM25+rerank + Contextual Retrieval** | BM25 抓精确串（场馆名/编号），dense 抓语义，rerank 精排 |
| 高频增量知识库 | LightRAG / Contextual Retrieval | GraphRAG/RAPTOR 增量差 |

> **对本产品的判断（重要）**：本产品的检索是**"本周末 + 某城市 + 某品类 + 地理范围内的活动/POI"**——这是**事实精确检索 + 地理/时间强过滤**，不是全局理解或多跳推理。因此：
> - **图 RAG（GraphRAG/LightRAG/HippoRAG）在本产品是过度设计**，还会引入图数据库运维、增量成本、可解释性下降，与"轻后端/证据优先"双双冲突。
> - **真正该采纳的是 Contextual Retrieval 思想 + 向量+BM25 混合 + rerank**——用极低成本把活动检索的召回质量拉满，且完全可溯源。
> - **Self-RAG/CRAG 的"按需检索 + 自评证据"思想**可融入 Provenance Guard 与降级设计（检索不足时明确 unknown + 官方入口，正是 CRAG 的"Incorrect→兜底"）。

---

## 4. 向量与检索基础设施：索引算法、向量库、混合检索与重排

方法论落地需要存储与索引基座。本节先讲通用的 ANN 索引原理与检索质量技术，再逐一对比向量库。

### 4.1 ANN 索引算法原理与权衡

- **HNSW（分层可导航小世界图）**：多层图，查询从顶层贪心下降逐层逼近最近邻。关键参数 `m`（出边数）/ `ef_construction`（构建质量）/ `ef_search`（查询召回↔延迟）。**低延迟高召回，内存内 ANN 的事实标准**；代价是**图常驻内存（比 IVFFlat 多占 2~5×）**，受 RAM 约束。
- **IVF / IVFFlat（倒排文件）**：k-means 聚成 `nlist` 簇，查询只在最近 `nprobe` 簇内比对。**构建快、内存省**；代价是需代表性训练数据、增删后聚类退化需重建。
- **DiskANN / StreamingDiskANN（磁盘图索引，源自微软 Vamana）**：内存只留**量化向量**做快速距离估算，图与全精度向量落 SSD，遍历时按需读盘做 **rescore**。**单机承载数千万~十亿级，内存占用远低于 HNSW**——即 pgvectorscale 的 StreamingDiskANN。
- 其它：**SCANN**（Google，CPU 高吞吐）、**CAGRA**（NVIDIA GPU 图索引）、**FLAT**（暴力，召回 100%，仅小数据/基准）。

| 维度 | HNSW | IVF(Flat) | DiskANN |
|---|---|---|---|
| 延迟 | 最低 | 中 | 中（含磁盘 IO） |
| 内存 | 高（图常驻） | 低~中 | 低（量化常驻，全精度落盘） |
| 规模上限 | 受 RAM 约束 | 中大 | 大（单机十亿级） |
| 增量更新 | 好 | 一般（重建质心） | 好（Streaming 变体） |

### 4.2 检索质量技术

- **Hybrid Search（稠密+稀疏）**：dense 擅长语义/近义/跨语种，但对**精确关键词、专有名词、编号**召回弱；BM25/稀疏擅长精确匹配。二者互补，用 **RRF** 或加权分数融合。
- **量化（Quantization）**：SQ（float32→int8，内存 −~4×，精度损失小）、PQ（子段码本，−~16-64×，损失较大）、BQ（每维 1 bit，−~32×，仅高维对称 embedding）。工程上均配 **oversampling + 全精度 rescore** 保召回。Milvus 2.6 引入 **RaBitQ 1-bit**（内存−72%、快 4×）；pgvectorscale 用 **SBQ（统计式二值量化）**。
- **Reranking**：bi-encoder（双塔，快、可预计算、精度有上限）作初排，cross-encoder（交叉编码器，精度高但不可预计算）只对 top-N（50~200）精排。**重排是 RAG 精度性价比最高的一环**。
- **中文 embedding 选型（2026）**：首选 **BGE-M3**（单模型同出 dense+sparse+ColBERT 多向量，1024 维，8192 上下文，开源自托管，**混合检索一站式**）；效果上限选 **Qwen3-Embedding（0.6B/4B/8B）**（8B 曾登 MTEB 多语种榜 70.58）；托管省心选 OpenAI `text-embedding-3-large`（可降维）或 Jina v3。重排统一用 **BGE-reranker-v2-m3**（自托管）或 Cohere Rerank 3.5（托管）。铁律：**入库/查询必须同模型同维度，换模型必须全量重嵌**。

### 4.3 五大向量库逐一剖析

**pgvector / pgvectorscale**（[pgvector](https://github.com/pgvector/pgvector) / [pgvectorscale](https://github.com/timescale/pgvectorscale)）：为 PostgreSQL 增加 `vector` 类型与距离算子（`<->` L2 / `<=>` 余弦 / `<#>` 内积），提供 HNSW / IVFFlat；pgvectorscale（Rust）追加 **StreamingDiskANN + SBQ + 标签过滤（Filtered DiskANN）**。**核心优势：向量列与任意 PG 列、PostGIS 几何列同表**——一条 SQL 内 `WHERE ST_DWithin(geom, :pt, :r) AND category='展览' ORDER BY embedding <=> :q` 完成**地理+结构化+语义三重条件，单事务**。官方基准：5000 万×768 维下相比 Pinecone s1 达 28× 低 p95、16× 高吞吐、成本−75%。**与本产品天然同构**。

**Milvus / Zilliz**（[milvus-io/milvus](https://github.com/milvus-io/milvus)）：存算分离分布式（Proxy/Query Node/Data Node/Streaming Node），**索引最全**（HNSW/IVF/DiskANN/SCANN/GPU CAGRA），2.6 引入 RaBitQ，原生稀疏+BM25+服务端混合检索，四级多租户。定位**十亿级、上万 QPS**；代价是架构复杂、运维重（etcd + 消息队列 + 对象存储），中小团队自托管门槛高。

**Qdrant**（[qdrant/qdrant](https://github.com/qdrant/qdrant)，Rust）：高性能低资源，**量化强（SQ/PQ/BQ + oversampling rescore）**、**payload 过滤强（filterable HNSW/ACORN 过滤感知检索）**、Query API 内置 prefetch + fusion(RRF/DBSF)。**从 pgvector 迁出的常见首选**，部署简单（单二进制）。

**Weaviate**（Go）：**模块化 vectorizer**（入库/查询自动向量化）+ **招牌 hybrid**（BM25F + 向量，`fusionType` 可选 rankedFusion(RRF)/relativeScoreFusion）+ GraphQL。**一站式 RAG 友好**，代价是 GraphQL 学习曲线与模块化复杂度。

**Chroma / LanceDB**：前者嵌入式、API 极简（`pip install chromadb`），**原型/PoC 首选**；后者基于列式 Lance 格式、可跑在 S3、多模态友好，**适合嵌入式/数据湖**。两者在高并发/分布式在线服务能力弱于 Milvus/Qdrant。

### 4.4 向量库横向对比

| 维度 | pgvector(+scale) | Milvus/Zilliz | Qdrant | Weaviate | Chroma | LanceDB |
|---|---|---|---|---|---|---|
| 部署形态 | PG 扩展(内置) | 嵌入/单机/分布式/云 | 单机/分布式/云 | 单机/分布式/云 | 嵌入/单机 | 嵌入/serverless |
| 适合规模 | 百万~数千万 | 千万~十亿+ | 百万~亿级 | 百万~亿级 | 千~百万 | 百万~亿级(单机) |
| ANN 索引 | HNSW/IVFFlat/**DiskANN** | 最全+**GPU** | HNSW | HNSW/flat | HNSW | IVF_PQ |
| 标量过滤 | **SQL任意+PostGIS** | 字段/JSON | **强payload+过滤感知** | 条件过滤 | metadata | 标量 |
| 混合检索 | 需自建(SQL+BM25) | **原生** | **原生+RRF** | **原生BM25F+RRF** | 较新版 | 支持 |
| 分布式 | 弱(副本/Citus) | **原生强** | 中 | 中 | 弱 | 弱 |
| 运维成本 | **最低(复用PG)** | 高 | 中低 | 中 | 低 | 最低(嵌入) |
| 适用场景 | 已用PG+地理过滤的中小产品 | 超大规模高并发 | 高性价比专用向量服务 | 一站式RAG | 原型/PoC | 嵌入式/数据湖 |

> **对本产品的结论（重要）**：
> - **pgvector 足够，且能走很远。** 活动数据（地理+结构化+语义）与 PostGIS 同库，一条 SQL 完成"附近 3km + 展览类 + 语义相似"，无双写一致性问题——专用库反而需把地理/结构化数据再同步一份或回 PG JOIN。用户记忆规模更小，pgvector 绰绰有余。
> - **迁移触发条件（满足其一再评估）**：单表活跃向量 >5000 万~1 亿；QPS 持续 >数千且 p99 敏感；需原生分布式/存算分离；需服务端原生混合检索/GPU。届时首选 **Qdrant**，超大规模选 **Milvus**。
> - **设计预留**：数据模型从一开始就规范 `向量 ID / 业务主键 / embedding 版本字段`，为日后"PG 存业务 + 专用库存向量"双轨预留迁移空间。阶段路径：pgvector(HNSW) → pgvectorscale(DiskANN) → 外挂 Qdrant/Milvus。

---

## 5. AI 记忆框架深度剖析：读写演化遗忘

记忆框架与 RAG 的本质区别：**RAG 解决"把外部静态知识塞进上下文"（只读）；记忆框架额外解决写入决策（什么值得记）、状态演化（旧事实如何被新事实覆盖/失效）、时间性（何时为真）、主体归属（谁的记忆）**。

### 5.1 Mem0 —— 通用记忆层（注意两代算法已切换）

- 仓库：[mem0ai/mem0](https://github.com/mem0ai/mem0)（核心 `mem0/memory/main.py`）· 论文 arXiv:2504.19413

**记忆模型**：把记忆抽象为**扁平的事实（fact）列表**，每条是自然语言 + 向量 + 元数据，按 `user_id / agent_id / run_id` 三级作用域隔离。

**核心机制——两代算法（本次源码调研确认的关键事实）**：
- **（A）经典算法（v1.1/v2，论文与多数二手资料描述的机制）**：**两次 LLM 调用**——先 extract（从对话抽候选事实），再 update（把候选事实与检索到的相似旧记忆交给 LLM，输出 `ADD / UPDATE / DELETE / NOOP`）。DELETE 就是其**冲突消解**（新事实与旧记忆矛盾→删旧）。
- **（B）新算法（最新 OSS 版，Single-Pass ADD-only）**：官方迁移指南明确改为**单次 LLM 调用、只返回 ADD**（extract all distinct new facts），**冲突消解从"写时删除"改为"检索时排序"**（新旧事实共存，最相关/最新的浮顶）。**已知副作用**（issue #4956）：过期/矛盾事实可能同时被检索到，社区在规划 `max_memories` 压缩（#5850）。另：**外接图存储（Neo4j 等 ~4000 行）被整体移除**，改为向量库内建实体链接。

**架构师提示（对本产品至关重要）**：若产品依赖"旧偏好被新偏好覆盖"（如"以前喜欢安静咖啡馆，现在改喜欢热闹酒吧"），**旧 v1.1 的 UPDATE/DELETE 语义更省心**；新版则需在检索层用时间戳/threshold 兜底。评测（新算法口径）：LoCoMo 71.4→91.6、LongMemEval 67.8→93.4。

### 5.2 Zep / Graphiti —— 时序知识图谱记忆（时间正确性一等公民）

- Graphiti 仓库：[getzep/graphiti](https://github.com/getzep/graphiti) · 论文 arXiv:2501.13956

**记忆模型**：建成**时序知识图谱**，三类节点 `EpisodicNode`（原始输入）/ `EntityNode`（实体，带 `name_embedding` 与 `summary`）/ `CommunityNode`（社区聚类摘要）。**双时间轴（bi-temporal）是核心武器**：每条 `EntityEdge` 同时携 `valid_at`/`invalid_at`（事件时间：现实中何时为真）与 `created_at`/`expired_at`（系统时间：系统何时知道）。

**代码级洞察：temporal invalidation**（`utils/maintenance/edge_operations.py`）：LLM 抽三元组→对新边做**两路混合检索（RRF）**找"重复候选"与"矛盾候选（edge_invalidation_candidates）"→ 当 LLM 判定新旧边矛盾时，**不删除旧边，而是给它设 `invalid_at`/`expired_at`**。这实现**无损遗忘**：既能答"现在为真的事实"，也能答"过去某时点为真的事实"——这是与 Mem0 最本质的差异。检索为三路混合（语义+BM25+图 BFS）+ 时点过滤。

**评测（含争议标注）**：DMR 94.8% vs MemGPT 93.4%；LongMemEval 相对提升最高约 18.5%；**LoCoMo 84% 存在第三方争议（复现约 58.44%）**——跨厂商基准存在口径战争，需自有数据复测。

### 5.3 Letta（原 MemGPT）—— 操作系统式虚拟上下文记忆

- 仓库：[letta-ai/letta](https://github.com/letta-ai/letta)（`letta/schemas/memory.py`）· 论文 arXiv:2310.08560

**记忆模型（OS 类比三层）**：**Core memory**（常驻上下文的 memory blocks，如 `persona`/`human`，容量有限、始终可见、**可被 agent 自我编辑**→ 对应多轮对话状态/工作记忆）、**Recall memory**（对话历史，可搜回）、**Archival memory**（无限容量外部向量库）。

**代码级洞察：self-editing memory**：`Memory` 由若干 `Block`（`label/value/description/limit/read_only`）组成；编译进上下文时**把 `chars_current/chars_limit` 一并渲染给模型**（agent 知道 block 还剩多少空间）；agent 通过**函数调用**（`core_memory_append`/`core_memory_replace`，新版支持带行号的 `memory_insert`/`memory_replace`）改自己的核心记忆。上下文接近满时触发 memory pressure，类 OS 换页。记忆质量高度依赖模型的工具调用能力（弱模型编辑易出错）。

### 5.4 LangMem（LangChain）—— 记忆 SDK

- 仓库：[langchain-ai/langmem](https://github.com/langchain-ai/langmem)

**记忆模型（认知科学三分法）**：Semantic（事实/偏好）/ Episodic（过去经历）/ Procedural（行为规则，常落在 system prompt 优化）。**不自带存储，依赖 LangGraph 的 `BaseStore`**（`InMemoryStore`/`AsyncPostgresStore`，带向量索引）。两种写入时机：**hot path**（对话中 agent 用工具当场决定记什么）与 **background**（会话后 memory manager 异步抽取/合并）。核心工具 `create_manage_memory_tool` / `create_search_memory_tool`；程序性记忆用 `create_prompt_optimizer`（根据反馈改写 system prompt）。**强绑 LangGraph、本身较薄**（组合 store + LLM）。

### 5.5 Cognee —— ECL 知识图谱管道

- 仓库：[topoteretes/cognee](https://github.com/topoteretes/cognee)，定位明确"replaces traditional RAG with an ECL pipeline"。

**ECL 管道**：`add()`（Extract：吸收任意格式数据、分块）→ `cognify()`（Cognify：LLM 抽实体/关系建知识图谱，节点/边表为强类型 `DataPoint`，写图库+向量库，可选时序管线）→ `search()`（Load：图+向量混合检索，多 search type）。存储为**图库+向量库+关系库三合一**（可插拔）。**面向"数据→知识图谱"最工程化，多模态输入**；代价是构图成本高、偏"知识库"而非"对话记忆"。

### 5.6 简要覆盖：MemoryScope（阿里）与 A-MEM

- **MemoryScope**：面向个人助理的长期记忆服务，内建**反思（reflection）→ insight → 再固化（re-consolidation）**（类人脑睡眠巩固），内建时间敏感检索（"上周""昨天"）；**适合中文个人助理**。
- **A-MEM**（arXiv:2502.12110）：基于 Zettelkasten，新 note 写入时 LLM 建**语义链接**并触发**记忆进化（改写被链接旧 note）**；研究原型，机制思想有启发性。

### 5.7 记忆框架横向对比

| 框架 | 记忆分类 | 存储 | 写入/更新 | 冲突消解 & 遗忘 | 时间建模 | 成熟度 | 最适场景 |
|---|---|---|---|---|---|---|---|
| **Mem0** | 语义为主+procedural | 向量库(15+)+内建实体 | 经典:双 LLM；**新版:单 LLM ADD-only** | 经典靠 DELETE；**新版靠检索排序** | 弱 | 高 | 用户长期偏好 |
| **Zep/Graphiti** | episodic+semantic+community | 图库+向量+BM25 | LLM 抽三元组→检重复&矛盾 | **temporal invalidation（不删可溯）** | **双时间轴一等公民** | 高 | 跨会话演化/溯源 |
| **Letta(MemGPT)** | core/recall/archival | 上下文+DB+向量 | agent 函数调用 self-edit | agent 主动改写 core block | 弱 | 高 | 多轮状态/角色一致 |
| **LangMem** | semantic/episodic/procedural | LangGraph BaseStore | hot path + background | manager 合并/更新 | 弱 | 中 | LangGraph 生态 agent |
| **Cognee** | 结构化知识(DataPoint) | 图+向量+关系库 | ECL: add→cognify→search | 实体归并/图更新 | 可选 | 中 | 文档/知识图谱型 |

### 5.8 RAG vs Memory：一句话区分

| 维度 | 传统 RAG | 记忆框架 |
|---|---|---|
| 数据性质 | 外部静态知识 | 交互中动态产生的信息 |
| 写入 | 离线批量、无"写决策" | **在线 + LLM 决策"该不该记"** |
| 更新/矛盾 | 不处理（都召回） | **显式消解**（DELETE/invalidation/evolution） |
| 时间性 | 基本无 | **一等公民**（双时间轴/时间检索） |
| 遗忘 | 无 | 有（删除/失效/压缩/衰减） |

一句话：**RAG 是"读"，Memory 是"读 + 写决策 + 冲突消解 + 时间性 + 遗忘"**。记忆框架的检索层复用 RAG 技术。

> **对本产品的判断（重要）**：
> - 本产品的**多轮会话状态已由 LangGraph Checkpointer 承载**（技术方案 §5.2），本质就是一套会话工作记忆——**不要用 Letta 去重复 LangGraph 已做好的事**。
> - 真正需新增的只有**跨会话的用户长期偏好**——用 **Mem0 思路的语义偏好记忆**即可（但建议自持写入决策以控制证据与脱敏）。
> - **切勿把“运营厂商官方基准”当真**：LoCoMo/DMR 存在口径争议（Zep 84% vs 复现 58%），选型必须在自有数据上复测。

---

## 6. 横向洞察：业界正在收敛的共性模式与关键权衡

看完四大类四十多个项目，以下是跨项目提炼的、对选型真正有指导意义的共性与权衡。

### 6.1 五个正在收敛的共性模式

1. **检索管线已收敛为事实标准：`稠密召回 + 稀疏/BM25 召回 → RRF 融合 → cross-encoder 重排 → top-k`。** R2R（加权 RRF）、FastGPT（`concatWeightedRecallLists`）、Haystack/LlamaIndex（Joiner/FusionRetriever）、Milvus/Qdrant/Weaviate（服务端融合）实现思路高度一致，差异只在"在哪一层做融合"。**启示：本产品不必发明新轮子，照此管线在 PG 内实现即可。**
2. **"证据/可溯源"正在从加分项变成必选项。** Anthropic Contextual Retrieval 保留上下文、GraphRAG 的 community report 带来源、Graphiti 的 episode 可溯源、Mem0/LangMem 的 source 字段——业界共识是"不可溯源的检索不可信"。**本产品的 Provenance Guard 与六态 verification_status 正是这一趋势的激进版，方向领先。**
3. **“单一后端”正在成为中小团队的理性选择。** R2R 把向量+全文+图谱全放 Postgres，LightRAG 可用 PG 一体化四类存储——**反对"为每个能力配一个专用中间件"的风潮正在兴起**，与本产品"轻后端"不谋而合。
4. **图方法与向量方法是互补而非替代，按查询类型分工。** 全局/多跳→图（GraphRAG/HippoRAG）；事实精确→向量+BM25+rerank。在事实检索场景硬上图，是赔本买卖。
5. **记忆正在从“扁平事实”走向“时间性与演化”，但工程上仍应克制。** Graphiti 的双时间轴、Mem0 的 ADD-only→compaction 之争，本质都在回答"旧事实怎么办"。但对本产品，**偏好漂移频率低**，用时间戳 + 最新优先即可，无需引入双时间轴图。

### 6.2 关键权衡一览

| 权衡轴 | 一端 | 另一端 | 本产品的选择 |
|---|---|---|---|
| 检索智能 vs 成本 | 图 RAG/Agentic（贵、慢、黑盒） | 向量+BM25（便宜、快、可解释） | **后者**（事实检索为主） |
| 记忆丰富 vs 可控 | Letta/Graphiti（自主、多机制） | Mem0/自研（简单、可控证据） | **后者**（证据/脱敏可控） |
| 后端能力 vs 运维 | Milvus/Neo4j（强、重） | pgvector（够用、零新增运维） | **后者**（轻后端） |
| 时间正确性 vs 简洁 | Graphiti 双时间轴 | 时间戳+最新优先 | **后者**（偏好漂移低频） |
| 召回质量 vs 预处理成本 | Contextual Retrieval（+LLM 预处理） | 纯向量（无预处理） | **前者（选性用于活动摘要）** |

### 6.3 反模式清单（本产品要主动规避）

- ❌ **为了"看起来先进"而引入 GraphRAG/图数据库**——本产品无全局理解/多跳需求，只会徒增成本与运维。
- ❌ **把 LLM 生成物直接当事实写入记忆/活动库**——违反证据优先，一票否决。交通/票价/余票字段永远不得以 LLM 为来源。
- ❌ **用重型记忆框架重复 LangGraph 已有的会话状态能力**——Checkpointer 已是会话工作记忆。
- ❌ **盲信跨厂商 leaderboard 选型**——LoCoMo/DMR 口径争议大，必须自有数据复测。
- ❌ **为活动库上重型文档解析（DeepDoc 级）**——数据源是网页/结构化，正文清洗已足够。

---

## 7. 面向「周末去哪儿」的技术选型（落地方案）

前六节是"业界全景"，本节是"我们怎么选"。所有决策都回到第 1 节的三个硬过滤器（证据优先 / 轻后端 / 数据主权），并无缝嵌入现有的 `LangGraph + PostgreSQL(PostGIS/pgvector) + Redis` 架构。

### 7.1 需求 → 技术 总映射

| 产品需求 | 属于 RAG 还是记忆 | 选型结论 | 一句话理由 |
|---|---|---|---|
| 当周活动检索 | RAG | **PG 内混合检索（向量 `<=>` + PG 全文/`pg_search` BM25）+ 加权 RRF + BGE rerank + 地理/时间过滤** | 事实精确+强过滤，与 PostGIS 同库，可溯源 |
| 目的地发现 | RAG（结构化为主） | 城市档案结构化查询 + 活动聚合打分 | 确定性计算，无需向量花哨 |
| 餐饮/POI 推荐 | RAG | 高德 POI + 用户 BYO 链接，同一混合检索管线 | 动线上的地理+语义 |
| 活动正文抽取质量 | RAG 预处理 | **Contextual Retrieval 思想（给活动 chunk 前置城市/场馆/时间上下文）** | 低成本拉高召回，与情报流水线契合 |
| 单次规划多轮状态 | 记忆（会话工作） | **直接用 LangGraph Checkpointer（已有）** | 不重复造轮子 |
| 用户长期偏好 | 记忆（语义长期） | **自研轻量偏好记忆（Mem0 思路，PG 存）+ 自持写入决策** | 控制证据/脱敏/覆盖，不引入新中间件 |
| 多人聚合约束 | 记忆（会话+脱敏） | LangGraph state 内聚合（已有 `aggregate_party`） | 隐私优先，只存聚合值 |
| 用户收藏链接池 | 记忆（资产） | 归入活动/POI 候选池（带 user_provided 证据） | 复用活动表与证据体系 |

> **一句话选型**：本产品**不引入任何 RAG/记忆框架作为核心**，而是**在现有 PG + LangGraph 上，借鉴 R2R 的单库混合检索、Contextual Retrieval 的上下文增强、Mem0 的偏好抽取思路，自研一层轻量、可溯源、可脱敏的检索与记忆能力**。框架作为思想来源，而非运行时依赖。

### 7.2 选型决策与落选理由

| 候选 | 是否采纳 | 理由 |
|---|---|---|
| **pgvector(+pgvectorscale)** | ✅ 采纳（向量底座） | 与 PostGIS/结构化同库，地理+语义单 SQL，零新增运维，数千万级足用 |
| **BGE-M3 + BGE-reranker-v2-m3** | ✅ 采纳（模型） | 中文强、开源自托管、单模型预留 sparse 供未来混合；rerank 提升推荐准确性性价比最高 |
| **R2R 式加权 RRF 混合检索** | ✅ 借鉴实现 | 其 `hybrid_search` SQL + 加权 RRF 几乎就是本产品应有的检索层 |
| **Contextual Retrieval** | ✅ 采纳思想（选性） | 给活动 chunk 前置上下文，低成本拉高召回；只对长页/多活动页启用 |
| **Mem0**（思路） | ✅ 借鉴思路，❗不直接引依赖 | 偏好抽取+语义检索思路好；但新版 ADD-only 不利于"偏好覆盖"，且需自控证据/脱敏，故自研轻量层更可控 |
| **Self-RAG / CRAG 思想** | ✅ 采纳思想 | "按需检索 + 自评证据支撑 + 不足则兜底"融入 Provenance Guard 与降级 |
| Milvus / Qdrant / Weaviate | ⏸ 暂不（预留迁移） | 规模未到、引入新中间件与"轻后端"冲突；触发条件到再外挂 |
| GraphRAG / LightRAG / HippoRAG | ❌ 不采纳 | 无全局理解/多跳需求，图库运维+增量成本与硬约束冲突（过度设计） |
| Letta / Graphiti / Cognee | ❌ 不采纳 | Letta 与 LangGraph 会话状态重叠；Graphiti/Cognee 需图库且偏重，偏好漂移低频用不上双时间轴 |
| Dify / FastGPT / RAGFlow 作为核心 | ❌ 不采纳 | 黑盒配置无法表达六态 verification_status + 字段级来源白名单（与 §3.1 对低代码平台的判断一致） |

### 7.3 目标架构：在现有架构上的“检索与记忆”增量

下图只画**相对技术方案 v1 新增/强化**的部分（➕ 为新增，◎ 为已有）：

```text
◎ LangGraph 状态机（编排）
   │  会话工作记忆 = Checkpointer(Postgres)（◎ 已有，无需新记忆框架）
   │
   ├─▶ ➕ 检索服务 RetrievalService（PG 内，无新中间件）
   │     ① dense：embedding <=> （pgvector HNSW）
   │     ② sparse：PG 全文 tsvector/ts_rank 或 pg_search(BM25)
   │     ③ 加权 RRF 融合（semantic_weight/full_text_weight, rrf_k）
   │     ④ BGE-reranker 精排 top-N→top-k
   │     ⑤ 地理/时间/品类过滤：ST_DWithin + start_at + category（同一 SQL）
   │     ⑥ 每条结果带出 Evidence（source_url/fetched_at/verification_status）
   │
   ├─▶ ➕ 用户偏好记忆 PreferenceMemory（PG 存，轻量自研）
   │     ① 写入：会话结束后小模型抽取偏好事实（脱敏后）
   │     ② 冲突：同一偏好维度取最新（时间戳覆盖，避免新旧共存）
   │     ③ 检索：规划起始时按 user_id 召回，注入约束解析
   │     ④ 作用域：user_id 隔离，不存精确地址/预算上限
   │
   ◎ 活动情报流水线（异步，Celery）
         ➕ 抽取阶段增加 Contextual 前置（城市/场馆/时间上下文）
         ◎ 写入 activity 表（已有 embedding VECTOR(1024)）
```

**部署增量 = 零新服务**：检索与偏好记忆都是 Planner Service 内的模块 + PG 表/索引，**不新增 Neo4j/Milvus/记忆服务**。自托管 embedding/reranker 模型（BGE-M3 / bge-reranker-v2-m3）可走一个轻量推理容器或百炼/第三方 API。

### 7.4 活动检索层的具体设计（可直接开工）

**一条 SQL 完成"地理+时间+品类过滤 + 向量语义"**（候回阶段，再交给应用层与 BM25 做 RRF）：

```sql
-- dense 候回：同一事务内完成地理/时间/品类硬过滤 + 语义排序
SELECT id, title, venue, start_at, verification_status, source_url,
       embedding <=> :q_vec AS dist
FROM activity
WHERE city_code = :city
  AND start_at >= :weekend_start AND start_at < :weekend_end
  AND category = ANY(:categories)
  AND ST_DWithin(location, :center::geography, :radius_m)
  AND verification_status IN ('official_source_confirmed','public_source_observed')
ORDER BY embedding <=> :q_vec
LIMIT :dense_k;      -- 与 sparse(BM25) 候回在应用层做加权 RRF
```

**加权 RRF 融合（仿 R2R 实现）**：`rrf = (w_sem/(k+rank_sem) + w_ft/(k+rank_ft)) / (w_sem+w_ft)`，k=60；融合后取 top-N 过 BGE-reranker 精排得 top-k。

**与证据体系的绑定（不可商量）**：
- 检索结果每条必携 `verification_status`；`official_source_confirmed` 才可作核心活动（对应 §8.5 定级）。
- **检索层不生成任何事实**：只做召回+排序，不调 LLM 改写事实字段（交通/票价字段永远不进检索生成）。
- **降级（CRAG 思想）**：若混合检索命中不足或均为低可信来源→输出"搜索关键词 + 官方来源清单"并标注 unknown（对应 PRD 韧性表）。

**Contextual Retrieval 落地（选性，情报流水线抽取阶段）**：对长页/含多活动的官方页，抽取时用小模型为每个活动 chunk 前置一句上下文（"本活动属于{城市}{场馆}，时间{周末}"）再 embedding，显著提升"北京 本周末 展览"这类查询的召回；成本靠 prompt caching 控制。

### 7.5 用户记忆层的具体设计（轻量、可控、可溯源）

**为什么自研而非直接上 Mem0**：本产品需要（a）写入前强制脱敏（§7.2 边界）；（b）偏好可被新偏好覆盖（新版 Mem0 ADD-only 不利）；（c）不新增依赖。这三点自研一个 PG 表 + 小模型抽取即可满足，比引入框架更可控。

```sql
CREATE TABLE user_preference (
  id           BIGSERIAL PRIMARY KEY,
  user_id      BIGINT NOT NULL,
  dimension    TEXT NOT NULL,      -- cuisine/budget_band/radius/night_train/flight/taboo
  value        JSONB NOT NULL,     -- 脱敏后的区间/枚举，不存精确地址/预算上限
  embedding    VECTOR(1024),       -- 语义检索（可选）
  source       TEXT NOT NULL,      -- user_stated / inferred_from_behavior
  confidence   REAL DEFAULT 0.5,
  updated_at   TIMESTAMPTZ DEFAULT now(),
  UNIQUE (user_id, dimension)      -- 同维度取最新：写时 upsert 覆盖
);
```

写入（background，借 LangMem 的 hot/background 划分）：会话结束后用小模型从对话抽偏好事实 → `redact()` 脱敏 → 按 `(user_id, dimension)` upsert（同维度新值覆盖旧值，天然解决"旧偏好、新偏好共存"）。检索：规划起始时按 user_id 拉全部偏好注入约束解析。**会话状态（本次预算/人数/已否决城市）一律走 LangGraph state，不进长期记忆**。

---

### 7.6 与证据优先/防幻觉的结合（本产品的灵魂）

检索与记忆一旦引入，**新的幻觉风险也随之引入**。本产品必须把两者纳入 Provenance Guard（技术方案 §7.3）的管辖：

| 风险点 | 防护机制 |
|---|---|
| 检索结果被当“已确认”展示 | 检索结果强制携 `verification_status`，UI 按状态渲染；仅 `official_source_confirmed` 可作核心活动 |
| 记忆把 LLM 推断当事实存入 | 偏好表 `source` 字段区分 `user_stated`/`inferred_from_behavior`；推断类低 `confidence`，仅作排序信号不作硬事实 |
| 跨交通/票价字段的幻觉 | 检索/记忆**均不触碰 `train.*`/`flight.*`/`*.availability`**；这些字段只能来自用户回填/官方（沿用 FIELD_SOURCE_POLICY 白名单） |
| 检索不足时编造 | CRAG 式降级：命中不足→输出关键词+官方入口并标 unknown，**绝不用相似活动凑数** |
| 过期信息 | activity `expires_at` 到期下架；偏好 `updated_at` 参与衰减（过久偏好降权或重确认） |

**一条 CI 断言（延续技术方案 §7.3 闸门三）**：任何进入 Trip Bundle 的检索/记忆派生字段，若 `source_type == 'llm'` 且落在交通/票价/余票白名单外，直接报错——对应 PRD 硬 KPI“未确认字段被错误展为已确认 = 0”。

### 7.7 分阶段落地（映射 PRD/技术方案路线图）

| 阶段 | 检索层 | 记忆层 |
|---|---|---|
| **v0.1 跑通闭环** | PG 向量(HNSW) + PG 全文 → 加权 RRF；BGE-M3 入库；BGE-reranker 精排；地理/时间/品类过滤；结果带证据 | 仅 LangGraph 会话状态（无长期记忆） |
| **v0.2 协作与效率** | Contextual 前置增强；pg_search(BM25) 替代 tsvector；多人聚合检索 | ➕ 用户偏好记忆上线（user_preference 表 + background 抽取 + 脱敏）；收藏链接池 |
| **v0.3 生态** | 规模接近阈值则切 pgvectorscale(DiskANN)；评估是否外挂 Qdrant | 偏好衰减/重确认；（若真需时间溯源才评估 Graphiti） |

### 7.8 相对技术方案 v1 的 BOM 增量

仅列**新增/明确化**项（其余沿用技术方案 §15 附录）：

| 层 | 新增/明确化选型 |
|---|---|
| Embedding | **BGE-M3**（1024 维 dense；预留 sparse），自托管或 API |
| Rerank | **BGE-reranker-v2-m3**（召回 top-100 → rerank → top-10） |
| 混合检索 | 初期 PG 全文(tsvector/ts_rank) + 向量应用层加权 RRF；规模大则 `pg_search`(BM25) |
| 向量规模升级 | 千万级切 **pgvectorscale StreamingDiskANN + SBQ** |
| 记忆 | **自研 user_preference 表 + 小模型抽取 + redact 脱敏**（不引入框架） |
| 预处理 | Contextual 上下文前置（情报流水线抽取阶段，靠 prompt caching） |

### 7.9 风险登记与规避

| 风险 | 影响 | 规避 |
|---|---|---|
| 中文检索召回不足 | 核心价值受损 | BGE-M3 + BM25 混合 + rerank + Contextual 前置；自有数据集回测调参 |
| 偏好记忆“越记越偏”/过时 | 推荐偏差 | 同维度 upsert 覆盖 + 衰减 + 重确认；低 confidence 不硬用 |
| 脱敏不彻底入记忆 | 隐私合规 | 写入前 `redact()` 强制；不存精确地址/预算上限/证件 |
| 向量规模增长超 pgvector 舒适区 | 延迟上升 | 阈值监控；pgvectorscale → 外挂 Qdrant 预留迁移路径 |
| embedding 换模型导致不一致 | 检索错乱 | `embedding 版本字段` + 换模型全量重嵌铁律 |
| 自托管推理模型运维 | 成本/可用性 | 小流量可先用百炼/第三方 rerank API，规模大再自托管 |

---

## 结语

本文从业界四大类四十多个 RAG/记忆项目的**真实源码与论文**出发，最终收敛到一条与产品硬约束一致的主轴：

> **能确定性过滤与计算的（地理/时间/品类）→ 交给 PG 内混合检索；**
> **需语义理解与召回的→ 交给向量+BM25+rerank，并带出证据；**
> **交互中产生、会演化的（会话状态/用户偏好）→ 会话交给 LangGraph、长期交给轻量可脱敏的偏好记忆；**
> **任何不可溯源的生成→ 一律降为 estimated/unknown。**

它不依赖任何重型 RAG/记忆框架，也不引入任何新中间件：**图 RAG 的智能用不上，专用向量库的规模还没到，重型记忆框架的会话状态 LangGraph 已经做好**。本产品的检索与记忆，本质是**在一个 Postgres 里，把业界已验证的“混合检索 + RRF + rerank + 上下文增强”管线和“可溯源、可脱敏、可覆盖的偏好记忆”做精、做足**——这才是与“中立、诚实、轻后端、证据优先”的产品灵魂最匹配的技术路径。

---

## 8. 附录：核心信源与源码索引

**RAG 框架**
- LlamaIndex: [run-llama/llama_index](https://github.com/run-llama/llama_index)（`core/ingestion/pipeline.py`, `core/schema.py`）
- Haystack: [deepset-ai/haystack](https://github.com/deepset-ai/haystack)（`core/pipeline/pipeline.py`）
- RAGFlow: [infiniflow/ragflow](https://github.com/infiniflow/ragflow)（`deepdoc/parser/pdf_parser.py`, `rag/app/naive.py`）
- R2R: [SciPhi-AI/R2R](https://github.com/SciPhi-AI/R2R)（`providers/database/postgres.py`, `chunks.py` 的 `hybrid_search`）
- Dify: [langgenius/dify](https://github.com/langgenius/dify)（`core/rag/datasource/retrieval_service.py`）；FastGPT: [labring/FastGPT](https://github.com/labring/FastGPT)（`core/dataset/search/`）

**高级/图 RAG**
- GraphRAG: [microsoft/graphrag](https://github.com/microsoft/graphrag)（arXiv:2404.16130）；LightRAG: [HKUDS/LightRAG](https://github.com/HKUDS/LightRAG)（arXiv:2410.05779）
- HippoRAG: [OSU-NLP-Group/HippoRAG](https://github.com/OSU-NLP-Group/HippoRAG)（arXiv:2502.14802）；RAPTOR: [parthsarthi03/raptor](https://github.com/parthsarthi03/raptor)（arXiv:2401.18059）
- Contextual Retrieval: [Anthropic 工程博客](https://www.anthropic.com/engineering/contextual-retrieval)；Self-RAG arXiv:2310.11511；CRAG arXiv:2401.15884；Adaptive RAG arXiv:2403.14403；ColBERT: [stanford-futuredata/ColBERT](https://github.com/stanford-futuredata/ColBERT)

**向量库与模型**
- pgvector: [pgvector/pgvector](https://github.com/pgvector/pgvector)；pgvectorscale: [timescale/pgvectorscale](https://github.com/timescale/pgvectorscale)
- Milvus: [milvus-io/milvus](https://github.com/milvus-io/milvus)；Qdrant: [qdrant/qdrant](https://github.com/qdrant/qdrant)；Weaviate/Chroma/LanceDB 官方文档
- BGE-M3/reranker: [FlagOpen/FlagEmbedding](https://github.com/FlagOpen/FlagEmbedding)；Qwen3-Embedding: [QwenLM/Qwen3-Embedding](https://github.com/QwenLM/Qwen3-Embedding)；MTEB: huggingface.co/spaces/mteb/leaderboard

**记忆框架**
- Mem0: [mem0ai/mem0](https://github.com/mem0ai/mem0)（`mem0/memory/main.py`, `configs/prompts.py`；arXiv:2504.19413）
- Graphiti: [getzep/graphiti](https://github.com/getzep/graphiti)（`utils/maintenance/edge_operations.py`；arXiv:2501.13956）
- Letta: [letta-ai/letta](https://github.com/letta-ai/letta)（`letta/schemas/memory.py`；arXiv:2310.08560）
- LangMem: [langchain-ai/langmem](https://github.com/langchain-ai/langmem)；Cognee: [topoteretes/cognee](https://github.com/topoteretes/cognee)；A-MEM: arXiv:2502.12110

> **数据口径提醒**：跨厂商基准（LoCoMo/LongMemEval/DMR）存在方法学争议（如 Zep LoCoMo 84% 与第三方复现 58.44% 的出入）；本文引用的性能数字（如 28×/-49%/-67%/70.58）为厂商官方基准或榜单口径，实际效果受数据集、维度、硬件、参数调优影响，**上线前应以自有数据复测为准**。
