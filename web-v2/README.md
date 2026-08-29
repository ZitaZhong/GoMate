# GoMate Web (web-v2) — 双模式前端

「周末去哪儿 / GoMate」的新前端：同时承载**跨城周末规划**（Trip Bundle）与**市内多人活动规划**（活动房间）两种模式，统一 Copilot 对话入口。

- 详细设计：`../技术方案/详细设计/DD-19_前端重构_双模式统一体验.md`
- 房间/多人协作后端契约：`../技术方案/详细设计/DD-18_GoMate_活动房间与市内多人协作.md`
- 旧版效果 Demo（保留）：`../web/index.html`

## 技术栈

| 层 | 选型 | 实际版本（见 package.json） |
|----|------|------|
| 框架 | Next.js (App Router) | 16.x |
| UI | React + TypeScript | 19.x / 5.x |
| 样式 | Tailwind CSS v4（CSS 内联主题，无 tailwind.config） | 4.x |
| 动效 | Framer Motion | 12.x |
| 状态 | Zustand + TanStack Query | 5.x |
| 图标 | Lucide React | - |

## 从零复现（Reproduce）

### 1. 环境要求

- Node.js >= 20（建议 LTS）
- npm（本项目用 npm，锁文件为 `package-lock.json`）
- Windows / macOS / Linux 均可

### 2. 安装与启动

```bash
cd web-v2
npm install        # 安装依赖（node_modules 约 450MB，已被 .gitignore 排除）
npm run dev        # 开发服务器，默认 http://localhost:3000
```

其他命令：

```bash
npm run build      # 生产构建
npm run start      # 启动生产服务
npm run lint       # ESLint 检查
```

### 3. 项目如何被初始化的（完整复现命令）

如果需要从空目录重建本项目，执行：

```bash
# 1) 脚手架（TypeScript + Tailwind + ESLint + App Router，npm，不用 src/ 目录）
npx --yes create-next-app@latest web-v2 --typescript --tailwind --eslint --app --import-alias="@/*" --use-npm --no-turbopack

# 2) 追加依赖
cd web-v2
npm install framer-motion zustand @tanstack/react-query lucide-react --save
```

然后应用本仓库的定制内容（相对脚手架的差异）：

| 文件 | 内容 |
|------|------|
| `app/globals.css` | GoMate 设计系统：基础色/强调色/证据六态 CSS 变量 + `@theme inline` 映射（Tailwind v4 方式） |
| `app/page.tsx` | 双模式首页（发起房间 / AI 对话 / 邀请码加入） |
| `components/evidence/FactField.tsx` | DD-03 证据六态渲染组件（硬 KPI：未确认误展为已确认 = 0） |
| `lib/sse.ts` | SSE 流式解析（跨城 + 市内两套事件通用） |
| `public/manifest.json` | PWA manifest（GoMate 品牌色） |

### 4. 后端联调

前端依赖 Python BFF（FastAPI，仓库根目录）：

```bash
# 仓库根目录（../）
uv sync
uv run uvicorn wheretogo.bff.app:app --reload --port 8000
```

开发期将前端 API 请求代理到 BFF：在 `next.config.ts` 中配置 `rewrites`（`/api/:path*` -> `http://localhost:8000/:path*`），或直接用环境变量 `NEXT_PUBLIC_API_BASE` 指向 BFF 地址。

跨城模式 SSE 事件（现有 BFF 已提供）：`node_output` / `interrupt` / `done` / `error` / `assistant_delta` / `clarify` / `research_progress`。
市内房间模式 API/SSE 事件（待后端实现，契约见 DD-18 §8）：`room_state` / `theme_result` / `activity_candidates` / `routes_ready` / `itinerary_generated`。

## 设计系统速查

配色定义在 `app/globals.css`（来源：GoMate PRD §13.2）：

- 基础：背景 `#F7F7F5`、卡片 `#FFFFFF`、主文字 `#2F3437`、次文字 `#787774`、边框 `#E3E2DF`
- 强调：绿 `#8FB59B`、黄 `#F2CD74`、蓝 `#86A7C7`、珊瑚 `#DF927C`、红 `#D96C6C`
- 证据六态：confirmed 绿 / official 蓝 / observed 灰 / estimated 黄 / unknown 浅灰 / expired 红

Tailwind v4 无 `tailwind.config.ts`，主题通过 `globals.css` 里的 `@theme inline` 声明，类名如 `text-evidence-confirmed`、`bg-accent-green` 直接可用。

## 硬性约束（不可违反）

1. **所有事实字段必须经 `FactField` 渲染**，禁止直接渲染 `value`（DD-03 硬 KPI）
2. **不做交易/支付 UI**（产品硬约束）
3. **分享卡默认脱敏**：不含精确地址/经纬度/个人预算/联系方式
4. 数据类字段（时间/预算/路线）不使用手写风字体
