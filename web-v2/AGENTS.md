<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

# GoMate web-v2 — Agent 工作指南

## 项目定位

「周末去哪儿 / GoMate」双模式前端：**跨城周末规划**（Trip Bundle）+ **市内多人活动规划**（活动房间），统一 Copilot 对话入口。上游设计文档：

- `../技术方案/详细设计/DD-19_前端重构_双模式统一体验.md` — 本项目的唯一前端技术契约（页面结构/组件设计/验收标准）
- `../技术方案/详细设计/DD-18_GoMate_活动房间与市内多人协作.md` — 房间模式后端 API/SSE 契约
- `../技术方案/详细设计/DD-13_TripBundle组装_前端展示_提醒.md` — 跨城模式 SSE 渲染契约与证据六态定义

## 环境与命令

```bash
npm install      # Node >= 20，npm（勿改用 yarn/pnpm，锁文件是 package-lock.json）
npm run dev      # http://localhost:3000
npm run build    # 生产构建（提交前应通过）
npm run lint     # ESLint（提交前应通过）
```

后端 BFF（FastAPI）在仓库根目录：`uv run uvicorn wheretogo.bff.app:app --port 8000`。

## 技术栈关键点（易踩坑）

1. **Tailwind CSS v4**：没有 `tailwind.config.ts`！主题在 `app/globals.css` 的 `@theme inline` 块中声明。新增颜色/字体先加 CSS 变量再映射，不要创建 config 文件。
2. **Next.js 16 App Router**：约定可能与训练数据不同，先查 `node_modules/next/dist/docs/`。
3. **SSE 解析**：统一用 `lib/sse.ts` 的 `parseSSE`/`consumeSSE`，不要另写 EventSource（需支持 POST 触发的流）。
4. 状态管理：Zustand（客户端全局）+ TanStack Query（服务端数据）；动效：Framer Motion（时长 200-500ms，见 GoMate PRD §13.5）。

## 硬性约束（违反即 bug）

1. **事实字段必须经 `components/evidence/FactField.tsx` 渲染**，禁止直接渲染 `{value, evidence}` 中的 value。这是 DD-03 硬 KPI「未确认误展为已确认 = 0」的前端落地。
2. **不做任何交易/支付/下单 UI**（产品硬约束「不做交易」）。
3. **分享类页面/卡片默认脱敏**：不得出现精确出发地址、经纬度、个人预算数字、联系方式。
4. **数据字段不用手写风字体**：时间/预算/路线等用系统无衬线；`--font-handwrite` 仅限装饰性标题（自托管 woff2 在 `public/fonts/`，经 `next/font/local` 挂载）。
5. **移动端优先**：交互区域 >= 44px，正文 >= 14px，禁止出现横向滚动。
6. **转盘结果服务端权威**：一律 `POST /rooms/{id}/theme/wheel` 取结果后前端纯动画，禁止本地随机选主题；`spins_left` 控制"一次反悔"（服务端强制，409=次数用完）。
7. **证据徽标对比度**：accent/证据色只做圆点/浅底色/边框，徽标文字一律 `text-primary`/`text-secondary`（≥4.5:1），禁止色相文字上白底。

## 目录约定

```
app/                页面（App Router）。房间流程在 app/room/[id]/*，跨城在 app/plan/[id]/*，对话在 app/chat
components/
  evidence/         证据六态（FactField/EvidenceBadge）—— 改动需对照 DD-03
  chat/             对话流组件（ChatPanel/MessageBubble/StreamingText/QuickCommands/ResearchProgress）
  cards/            结构化卡片（ActivityCard/CityCard/TransportCard/PlanCard/CardRouter/CommuteBar/FilterBar）
  room/             房间专用（MemberForm/便签墙/转盘/投票/GatheringInfo + useRoomGuard/usePolling hooks）
  itinerary/        时间轴行程（VerticalTimeline/ItineraryNode/BudgetSummary/ModifyInput/slotMapping）
  ui/               基础组件（Button/Input/Select/Tag/Modal/Skeleton）
lib/                types.ts（全部 API/SSE 类型）/ api.ts（typed client）/ sse.ts / store.ts（匿名会话）/ constants.ts
```

## 运行时契约要点

- **匿名会话**：`localStorage["gomate:room:{roomId}"] = {member_id, member_token, nickname}`（`lib/store.ts`），成员操作带 `member_token` 放 body
- **路由守卫**：房间页统一 `useRoomGuard(roomId, allowed[])`，按 `canonicalRoomPath`（DD-19 §5.3）重定向；EXPIRED → `/room/[id]` 只读页
- **多人同步**：非流式页 `usePolling` 5s 轮询 + 聚焦刷新；流式页（recommend/plan modify）走 SSE
- **对话链路**：`POST /plans/{pid}/chat` → decision（reply 进气泡）→ `auto_stream` 则 POST `/research-more`(SSE)、`restart_stream` 则 GET `/stream`(SSE)；`pid="new"` 首条带约束消息服务端自动建 plan，前端用返回的 `plan_id` 替换 URL
- **API 连接**：`lib/api.ts` base = `NEXT_PUBLIC_API_BASE ?? "/api"`。本地开发用 `.env.local` 设 `NEXT_PUBLIC_API_BASE=http://localhost:8000` **直连 BFF**（Next 16 dev 代理对慢请求/SSE 不可靠，BFF 已配 CORS 放行 localhost:3000）；生产删除 `.env.local` 回退同源 `/api` 代理。`next.config.ts` 的 `allowedDevOrigins` 必须保留（否则 127.0.0.1 访问时 hydration 静默失效）
- **bundle 恢复**：plan 页先进 `/plans/{id}/state`，state values 无 bundle 时回退 `GET /plans/{id}/bundle`（trip_bundles 表恢复，interrupt/done 仅落库）
- **对话页 URL**：chat-first 建 plan 后用原生 `history.replaceState` 更新 `?plan=`，**禁止 `router.replace`**（会触发 `key={plan}` 重挂载清空会话）

## SSE 事件速查

- 跨城（BFF 已有）：`assistant_delta` `clarify` `node_output` `progress`（图/深研进度；DD-15 文档名 `research_progress`，实现为 `progress`，前端两者兼容监听）`interrupt` `done` `error`
- 市内房间（BFF 已有，`bff/rooms.py`）：`room_state` `progress` `activity_candidates` `gathering` `member_routes` `itinerary` `interrupt` `revision_classified` `needs_confirmation` `no_change` `itinerary_updated` `done` `error`

## 配色（GoMate PRD §13.2，已在 globals.css 定义）

基础：bg `#F7F7F5` / card `#FFFFFF` / 主文字 `#2F3437` / 次文字 `#787774` / 边框 `#E3E2DF`
强调：green `#8FB59B` / yellow `#F2CD74` / blue `#86A7C7` / coral `#DF927C` / red `#D96C6C`
证据六态类名：`text-evidence-confirmed` 等（confirmed/official/observed/estimated/unknown/expired）

## Git 约定

- 特性分支：`feat/<语义化名称>`（如 `feat/gomate-dual-mode`）
- `node_modules/` 已在 `.gitignore`，勿提交

