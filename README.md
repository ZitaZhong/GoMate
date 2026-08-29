# GoMate — 周末出行决策 Agent

> 输入大家的时间、出发地和预算，系统帮你决定这个周末**去哪座城**（跨城模式）或**做什么活动**（市内模式），把当周活动实时调研、交通决策、多人协调、行程编排整合成一份可以直接发群执行的计划。

**三条产品硬约束**（贯穿全部代码与设计）：

1. **不做交易** — 只做决策层，查票/购票/订房一律深链跳官方平台
2. **证据优先** — 每条事实带来源/更新时间/确认状态（六态），AI 不知道就说不知道，绝不编造
3. **当周即研** — 活动信息每次请求实时深度研究（联网多源 → 官方核实 → 定级入库），不靠陈旧库存

---

## 1. 双模式产品形态

| | 跨城模式（周末去哪儿） | 市内模式（GoMate 活动房间） |
|---|---|---|
| 用户输入 | "周末想出去玩，预算2000" / "上海出发去北京" | "周六下午和朋友上海看展" / 创建活动房间 |
| 核心决策 | 去哪座城 + 火车/飞机门到门比较 | 做什么活动 + 多人通勤公平 |
| 多人协作 | 约束聚合（隐私脱敏分享） | 活动房间：邀请码加入、便签墙、主题转盘、投票 |
| 输出 | Weekend Trip Bundle（探索版→回填→确认版） | 半日/全日行程卡（集合点+每人路线+时间轴） |
| 模式路由 | DD-15 Copilot 意图分类自动判定（`cross_city` / `local`），或用户显式选择 | |

两种模式**共享**底层基础设施：证据护栏（DD-03）、外部 Provider（DD-04）、混合检索（DD-05）、活动情报流水线（DD-06）、长期记忆（DD-16）、实时深度研究（DD-17）。

## 2. 系统架构与前后端配合

```text
┌─────────────────────────────────────────────────────────┐
│  前端 web-v2/ (Next.js PWA, 端口 3000)                    │
│  · 双模式首页 / Copilot 对话流 / 房间流程 / 行程卡           │
│  · 所有事实字段经 FactField 组件渲染证据六态                 │
└──────────────┬──────────────────────────────────────────┘
               │ REST + SSE（流式事件）
┌──────────────▼──────────────────────────────────────────┐
│  BFF (FastAPI, src/wheretogo/bff/app.py, 端口 8000)      │
│  · POST /plans → SSE /plans/{id}/stream（跨城两段式）      │
│  · POST /plans/{id}/chat（Copilot 多轮对话）              │
│  · /rooms/*（市内房间模式，契约见 DD-18，实现中）            │
│  · /ui 挂载旧版效果 Demo（web/index.html）                 │
└──────────────┬──────────────────────────────────────────┘
               │ 驱动
┌──────────────▼──────────────────────────────────────────┐
│  编排层 (LangGraph 状态机, src/wheretogo/orchestration/)  │
│  parse→discover→research→transport→〔interrupt 等回填〕    │
│      →hotel→mobility→dining→timeline→validate→compose    │
│  · research 节点每轮强制触发 DD-17 实时深研（跨城+市内同源）  │
│  · checkpoint 持久化到 Postgres（跨天恢复）                │
└──────┬───────────────┬──────────────┬────────────────────┘
       │               │              │
  领域模块          检索服务         Provider 层
  (domain/)      (retrieval/       (providers/: 高德/和风/
  约束/交通/       混合检索+重排)      航班/搜索/LLM，无 key
  时间线/组装                        自动确定性兜底离线可跑)
       │               │              │
┌──────▼───────────────▼──────────────▼────────────────────┐
│  存储：PostgreSQL(PostGIS+pgvector, 端口5433)              │
│       + Redis(端口6380) — Docker 隔离实例，非默认端口        │
└──────────────────────────────────────────────────────────┘
```

**前后端配合的关键契约**：

- **SSE 事件流**：前端只按 `event` 类型渲染卡片，每个事实字段自带 `{value, evidence}`。
  - 跨城模式（已实现）：`node_output` / `interrupt`（探索版+回填表单）/ `done`（确认版）/ `error` / `assistant_delta` / `clarify` / `research_progress`
  - 市内房间模式（契约见 DD-18 §8，后端实现中）：`room_state` / `theme_result` / `activity_candidates` / `routes_ready` / `itinerary_generated`
- **证据六态**：`confirmed_by_user / official_source_confirmed / public_source_observed / estimated / unknown / expired` — 前端 `FactField` 组件是唯一渲染入口，硬 KPI「未确认误展为已确认 = 0」
- **两段式规划**：探索版（交通未确认，给决策框架）→ 用户去官方平台买票 → 回填（文本/截图/扩展）→ 确认版（逐小时时间线）

## 3. 从零跑起来（复现指南）

### 3.1 环境要求

- Python 3.11/3.12 + [uv](https://docs.astral.sh/uv/)
- Node.js >= 20 + npm（前端）
- Docker（数据库基础设施）

### 3.2 启动步骤（按顺序）

```bash
# ① 基础设施：PostgreSQL(PostGIS+pgvector) + Redis（隔离实例，非默认端口）
docker compose up -d
# 严禁改配置指向本地已有 PostgreSQL —— 本项目强制使用 5433/6380 隔离实例

# ② Python 依赖
uv sync                  # 基础依赖（含 dev 时：uv sync --extra dev）

# ③ 环境变量
cp .env.example .env     # Windows: copy .env.example .env
# 所有外部 API key 均可留空 → Provider 自动走确定性兜底，离线可跑通全流程
# 要真实效果再填：WTG_LLM_API_KEY / WTG_AMAP_KEY / WTG_SEARCH_API_KEY 等

# ④ 数据库迁移 + 种子数据（15 城城市档案 + 来源注册表）
uv run alembic upgrade head
uv run python -m wheretogo.seeds.loader

# ⑤ 启动后端 BFF（端口 8000）
uv run uvicorn wheretogo.bff.app:app --reload --port 8000
# 旧版效果 Demo 直接可用：http://localhost:8000/ui

# ⑥ 启动新前端（端口 3000，另开终端）
cd web-v2
npm install
npm run dev              # http://localhost:3000
```

### 3.3 验证

```bash
curl http://localhost:8000/health        # {"ok": true}
uv run pytest                            # 全量测试（离线，无需任何 key）
uv run ruff check .                      # 代码检查
```

## 4. 目录导览

| 路径 | 内容 |
|------|------|
| `PRD/` | 产品需求：`周末去哪儿_产品方案_对外版.md`（跨城）+ `GoMate_PRD.docx`（市内多人） |
| `技术方案/详细设计/` | **DD-00 ~ DD-19 详细设计**（先读 DD-00 总纲建立全局，再按需深入） |
| `技术方案/对话意图理解与DeepResearch重构分析设计_v1.md` | Copilot 对话控制层与深研重构分析（尚未实施的重构建议） |
| `src/wheretogo/` | Python 后端：`bff/`(API) `orchestration/`(LangGraph) `domain/`(领域) `retrieval/`(检索) `intel/`(情报流水线) `research/`(深研) `providers/`(外部服务) `copilot/`(对话) `memory/`(记忆) |
| `web-v2/` | **新前端**（Next.js 双模式，见 `web-v2/README.md` 与 `web-v2/AGENTS.md`） |
| `web/index.html` | 旧版效果 Demo（保留，BFF 挂载在 `/ui`） |
| `extension/` | 浏览器扩展 MV3（回填最后一公里，DD-14） |
| `migrations/` | Alembic 数据库迁移 |
| `seeds/` | 种子数据（15 城城市档案） |
| `tests/` | 按 DD 编号组织的测试套件（离线可跑） |
| `竞品与开源技术调研/` | 竞品/开源/RAG 框架深度调研报告 |

**文档阅读路径**（人或 Agent 首次进入项目）：

1. 本 README（全局） → 2. `技术方案/详细设计/DD-00_总体详细设计_集成与联调.md`（模块地图+数据流+接口索引） → 3. 按任务查对应 DD 文档 → 4. 前端任务再读 `web-v2/AGENTS.md`

## 5. 开发约定

- **Git**：特性分支 `feat/<语义化名称>`；`web-v2/node_modules/` 已 gitignore
- **Python**：ruff（line-length=100）；测试离线运行（conftest 强制覆盖 .env）
- **数据库**：只用 Docker 隔离实例（5433/6380），TIMESTAMPTZ 统一 UTC 存储，展示层转 Asia/Shanghai
- **证据红线**：交通票价/余票永不由 LLM 生成（DD-03 闸三，CI 门禁）；所有产事实模块过 `enforce_provenance`
- **前端红线**：见 `web-v2/AGENTS.md` 硬性约束一节
- **真实端到端验证（必须）**：任何功能新增或修改，除 `pytest` 全部通过外，还必须在**真实运行的服务上**（docker compose 基础设施 + uvicorn BFF + 前端）实际走通受影响的完整用户流程（如：创建计划 → SSE 流 → 探索版 → 回填 → 确认版）并确认无报错、无降级、数据正确，才算完成；仅测试通过不算验收

## 6. 当前状态

| 能力 | 状态 |
|------|------|
| 跨城两段式规划（探索→回填→确认） | ✅ 端到端可跑（含 SSE、checkpoint 恢复） |
| 对话式 Copilot + 记忆 + 实时深研 | ✅ v2 已实现（DD-15/16/17） |
| 深研多轮迭代 + 跨轮偏好感知 | ✅ 已合入 main（research→reflect 回环，偏好跨轮生效） |
| 浏览器扩展回填 | ✅ MV3 骨架 |
| Copilot 对话控制层重构 | 📋 分析设计已出（见上方重构分析文档），尚未实施 |
| 市内房间模式后端（DD-18） | ✅ 已实现（15 端点 + SSE，联调通过） |
| 新前端 web-v2（DD-19） | ✅ 双模式全页面实现，真实服务联调通过（见 `测试报告/DD-19联调测试报告_2026-07-28.md`） |
| 旧版效果 Demo | ✅ `http://localhost:8000/ui` |
