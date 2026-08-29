# DD-13 Trip Bundle 组装 · 前端展示（克制）· 提醒系统 · 详细设计

**详细设计系列 · 领域节点 + 交付契约文档 · v1.0 · 2026 年 7 月**

> 本文定义 DD-02 图拓扑中的 **`compose` 节点**（PlanComposer，**最终闸**）与 **`await_booking` 期的 `compose_explore`**：把全链路产物组装为 **Trip Bundle（探索版 / 确认版）**，每字段带 `evidence`；**出稿前跑 DD-03 闸三 `assert_no_fabricated_transport`（最终闸）**，违规字段替换为「待你在官方平台确认」占位；写 `trip_bundles`（版本快照）。并定义**前端克制展示契约**（FactField 六态、城市卡/交通卡/计划卡静态渲染、分享卡脱敏）与**提醒系统**（Web Push VAPID / 邮件 / ICS 动态订阅 / 起售提醒入队）。
>
> **上游依据**：v1 §6.1（六态）/§6（Trip Bundle 探索版·确认版）/§8（提醒系统）/§9.7（ICS/Push）；v1.1 增补 B（前端克制，P0 仅证据可视）；DD-01 §8.6 `trip_bundles`、§8.7 `reminders`；DD-02 §5（`compose`=最终闸）/§6（interrupt payload）/§11（SSE 事件）；DD-03 §6（闸三）/§7（前端渲染契约）；DD-09 §3.2（`transport_options.presale`）；DD-12 `timeline_slots`（若存在）。
> **下游消费者**：BFF/前端（渲染 bundle 与六态）、DD-13 reminder worker（Celery beat 投递）、用户日历客户端（订阅 ICS）。
> **三条硬约束落地**：① **不做交易**（bundle 只呈现决策与官方入口，提醒只提示不代购）；② **证据优先**（compose 出稿前跑闸三**最终闸**，任何 `train.*`/`flight.*`/`*.availability` 的 `llm` 来源直接替换占位）；③ **轻后端重证据 + 前端极度克制**（本阶段前端**只做**证据六态可辨 + 结构化卡片静态渲染，**明确不做**预算滑杆/双栏交互地图/拖拽编辑/手绘图，地图=静态图/高德链接）。

---

## 1. 模块职责与边界（实现 `compose` + 前端展示 + 提醒）

| 项 | 说明 |
|---|---|
| **职责** | ① 实现 DD-02 `compose_explore`（交通确认前）与 `compose`（确认版，**最终闸**）：组装带 evidence 的 Trip Bundle，**出稿前跑 DD-03 闸三**，写 `trip_bundles`。② 定义前端**克制展示契约**：FactField 六态、城市卡/交通卡/计划卡静态渲染、分享卡脱敏、地图=静态图/高德链接。③ 提醒系统：Web Push（VAPID）/邮件/ICS 动态订阅，起售提醒由 DD-09 `presale` 计算入队（Celery beat），写 `reminders`。 |
| **边界内** | `compose_explore_bundle` / `compose_confirm_bundle`、bundle payload schema、闸三断言调用与占位替换、`trip_bundles` 落库、SSE 渲染契约（`node_output`/`interrupt`/`done`）、FactField 渲染映射、分享卡脱敏、`build_ics`、Web Push 投递、`enqueue_reminders`（含 presale/行前/证件等九类）、Celery beat 调度。 |
| **边界外** | 各产物的**值**从哪来（DD-05/06/08/09/11/12 各自产出并已带 evidence，本节点**透传不重造**）；证据定级规则（DD-03，本节点只**调用** `assert_no_fabricated_transport`，不改白名单）；交通门到门/起售时间计算（DD-09，本节点只**消费** `presale`）；时间线求解（DD-12，本节点只**消费** `timeline_slots`）；LangGraph 图拓扑/中断恢复（DD-02）。 |
| **架构位置** | v1 §4.1「编排层 · PlanComposer」＋「交付层（客户端 PWA / ICS / Push）」；DD-02 图节点 `validate → compose → END`，以及 `await_booking` 期的探索版产出。 |

**节点契约（对齐 DD-02 §5，逐字一致）**：

| 节点 | 读入 state 字段 | 写出 state 字段 | 调用（模块/Provider） | 受 Guard | 降级行为 |
|---|---|---|---|---|---|
| `await_booking`（compose_explore） | `transport_options`, `candidate_cities`, `constraints`, `activities`, `warnings` | `bundle`(explore) | DD-13 `compose_explore_bundle` | 是（每字段带 evidence） | AI 异常→规则模板简化探索版 |
| `compose`（最终闸） | 全部产物（bookings/hotel_area/dining/local_routes/timeline/validation/…） | `bundle`(confirm) | DD-13 `compose_confirm_bundle`；**出稿前跑 `assert_no_fabricated_transport`** | **是（最终闸）** | 闸三命中→占位替换；AI 异常→规则简化确认版 |

> **读写解耦铁律（DD-02 §5）**：`compose` 对 `activities` **只读**；对交通事实**不新造**，仅透传 DD-09 产出。提醒写 `reminders`、bundle 写 `trip_bundles`，是本节点**唯二**写业务表（DD-01 §13 归属矩阵）。

---

## 2. 设计目标与非目标

**目标**：
1. **最终闸兜底**：`compose` 出稿前 100% 跑 `assert_no_fabricated_transport`（DD-03 §6）；任何 `train.*`/`flight.*`/`*.availability` 来源为 `llm` → **直接替换为「待你在官方平台确认」占位**（而非抛错中断出稿），并计入硬 KPI 埋点。
2. **版本快照可追溯**：探索版/确认版各落一条 `trip_bundles`（不可变快照），BFF/前端与分享链接读快照，重规划产生新快照。
3. **前端极度克制**：只交付「证据六态可辨 + 结构化卡片静态渲染」；地图仅静态图/高德深链；**不做**滑杆/双栏地图/拖拽/手绘（§9 明确清单）。
4. **提醒零平台依赖**：ICS 动态订阅（`GET /plans/{id}/calendar.ics`，实时生成、零落盘）保证任何日历客户端可订阅；Web Push/邮件为增强通道。
5. **提醒诚实**：起售/复查等提醒只提示与附官方入口，**不代购、不查余票**；估算时点一律标「以官方平台当前页面为准」。

**非目标**：
- ❌ 不在 bundle 里生成任何票价/余票（交 DD-09 源头 + 本节点闸三双保险）。
- ❌ 不做交互式富前端（滑杆/双栏地图/拖拽/手绘富媒体，v1.1 增补 B 明确延后）。
- ❌ 不做交易/下单/代购/账户操作（提醒仅提示）。
- ❌ 不重造各领域产物的值与 evidence（只透传 + 组装 + 最终校验）。
- ❌ 不实现 DD-12 时间线求解（只消费 `timeline_slots`）；DD-12 不存在时用 `bookings + activities + dining + route_legs` 规则拼装降级时间线（§8）。

---

## 3. 数据模型（`trip_bundles` / `reminders` 引用 DD-01；bundle payload schema）

### 3.1 落库表引用（DD-01 §8.6 / §8.7，本节点**唯二**写表）

```sql
-- DD-01 §8.6 Trip Bundle 版本快照（本节点写；BFF/前端读）
-- trip_bundles(id, plan_id, version bundle_version, payload JSONB, created_at)
--   version ∈ {'explore','confirm'}；payload = 渲染就绪的完整 bundle（每字段含 evidence）
-- DD-01 §8.7 提醒（本节点写；DD-13 worker 读投递）
-- reminders(id, plan_id, type reminder_type, fire_at, channel reminder_channel,
--           payload JSONB, status TEXT, sent_at)
```

> 本节点**不新增列**。`payload` 结构由本文 §3.2/§3.3 权威定义；`reminders.payload` 结构由 §3.4 定义。落库前 `payload` 内所有 `evidence` 子对象必须符合 DD-01 §5 JSONB 规范（用 `Evidence.to_jsonb()`）。

### 3.2 Bundle payload schema —— 事实字段包装（BundleField，复用 DD-03 `Fact`）

bundle 内**每个对外事实**都用 `BundleField`（= DD-03 `Fact` 的落库形态：`value` + `evidence`）包装；纯结构性/文案字段（如卡片标题、清单项）不强制包装。

```jsonc
// BundleField —— 渲染就绪的事实字段（前端 FactField 直接吃）
{
  "value": "上海博物馆东馆「星耀中国」特展",   // 任意 JSON 值；unknown 时可为 null
  "evidence": {                                 // DD-01 §5 标准 JSONB（DD-03 Evidence）
    "source_type": "official_venue",
    "source_url": "https://...",
    "fetched_at": "2026-07-18T10:00:00Z",
    "verification_status": "official_source_confirmed",
    "confidence": 0.92,
    "note": null
  }
}
```

### 3.3 Bundle payload schema —— 探索版 / 确认版权威结构

```jsonc
// trip_bundles.payload —— 顶层信封（explore 与 confirm 共用外层，内层块按 version 裁剪）
{
  "schema_version": "bundle-1.0",
  "plan_id": "1024",
  "version": "explore",                 // explore | confirm（= trip_bundles.version）
  "generated_at": "2026-07-21T09:00:00+08:00",
  "stage": "await_booking",             // 与 plans.stage 一致（explore/await_booking/confirm）
  "title": "上海 → 北京 · 本周末双城计划",   // 结构性文案，非事实字段
  "summary": "高铁/飞机门到门接近，建议都比较；核心是上博东馆特展",
  "share": { "shareable": true, "desensitized": true },   // §7.2 分享卡脱敏标记

  // ===== 探索版块（version=explore 必有；confirm 也保留作为概览） =====
  "explore": {
    "destination":   { "value": "北京", "evidence": {...} },   // BundleField
    "theme":         "城市文化·展览",                            // 结构性文案
    "recommended_transport": {                                  // 透传 DD-09 recommended_mode+reason
      "mode": { "value": "compare", "evidence": {...} },
      "reason": "直线约1070km，高铁约4.5h、飞行约2.2h但门到门接近；两者都值得比较"
    },
    "depart_window": { "value": "周五 18:30 后 / 周六 06:00–08:00", "evidence": {...} },
    "return_window": { "value": "周日 17:00–20:00", "evidence": {...} },
    "budget_band":   { "value": {"min": 1800, "max": 2600, "currency": "CNY"}, "evidence": {...} },
    "core_activities": [                                        // 核心活动（透传 DD-05/06 evidence）
      { "title": {"value": "「星耀中国」特展", "evidence": {...}},
        "venue": {"value": "上海博物馆东馆", "evidence": {...}},
        "start_at": {"value": "2026-07-25T10:00:00+08:00", "evidence": {...}},
        "price_text": {"value": "￥60", "evidence": {...}},
        "booking_url": {"value": "https://...", "evidence": {...}},
        "map": {"static_img_url": "https://.../static?...", "amap_url": "https://uri.amap.com/..."} }
    ],
    "lodging_area":  { "value": "东城·王府井/前门", "evidence": {...} },  // 区域级，不给门牌
    "transport_compare": { /* 透传 transport_options.candidates[i]（门到门/三方案/策略卡） */ },
    "todo_checklist": [                                         // 待确认清单（探索版核心交付）
      {"kind": "book_train", "text": "在 12306 确认周六早高铁车次与票价", "done": false},
      {"kind": "book_flight","text": "如选飞机，去航司/OTA 比价并确认行李", "done": false},
      {"kind": "book_hotel", "text": "确认东城住宿与免费取消截止时间", "done": false}
    ]
  },

  // ===== 确认版块（version=confirm 才有） =====
  "confirm": {
    "activities": [ /* 完整活动列表，每字段 BundleField（透传 DD-05/06） */ ],
    "dining":     [ /* 餐饮（透传 DD-11 dining_picks）：name/open_hours/phone 各带 evidence */ ],
    "local_routes":[ /* 市内路线（透传 DD-11 route_legs）：minutes/distance_m 带 evidence */ ],
    "timeline": [                                              // 逐小时时间线（透传 DD-12 timeline_slots）
      {"seq": 1, "start_at": {"value":"...","evidence":{...}}, "end_at": {"value":"...","evidence":{...}},
       "kind": "transport", "title": "G101 上海虹桥→北京南", "ref": {"table":"bookings","id": 88}}  // ref 由 DD-12 timeline_slots.ref_table/ref_id 映射
    ],
    "confirmed_cost": {                                        // 已确认花费（仅来自 bookings=confirmed_by_user）
      "items": [ {"label": "高铁往返", "amount_cents": 112400, "evidence": {...confirmed_by_user...}} ],
      "total_cents": 112400
    },
    "estimated_cost": {                                        // 预估待花（estimated，必须与已确认视觉可辨）
      "items": [ {"label": "餐饮", "amount_cents": 60000, "evidence": {...estimated...}} ],
      "total_cents": 60000
    },
    "risks": [                                                 // 风险提示（天气/返程紧/未确认项）
      {"level": "warn", "text": "周日 17:00 返程与末场活动间隔仅 40min，建议留缓冲",
       "evidence": {"source_type":"rule","verification_status":"estimated"}}
    ],
    "alternatives": [ {"for": "室外行程", "text": "若降雨改上博东馆室内动线", "evidence": {...}} ]
  },

  // ===== 横切：待确认清单 & 提醒预览（两版共用） =====
  "reminders_preview": [ /* enqueue_reminders 产出的可读摘要（§3.4 子集，供前端展示“将提醒你…”） */ ],
  "disclaimer": "票价/余票/起售时间以官方平台当前页面为准；带“估算/待确认”标记的字段非最终值"
}
```

> **占位约定**：闸三命中被替换的交通事实，`value` 记为字符串 `"待你在官方平台确认"`，`evidence.verification_status` 记为 `unknown`、`source_type` 保留原值、`note="闸三替换：交通事实禁止 LLM 生成"`（§5.3）。前端据 `unknown` 态渲染「暂无/请补充」。

### 3.4 `reminders.payload` schema（九类提醒统一结构）

```jsonc
// reminders.payload —— 投递侧（Push/Email/ICS）共用；type 见 DD-01 reminder_type
{
  "type": "presale",                          // presale|activity_booking|flight_recheck|
                                              // pre_trip_72h|weather_24h|doc_check|
                                              // hotel_cancel_deadline|activity_start|return_trip
  "title": "上海→北京 高铁起售提醒",
  "body": "周六早 06:00–08:00 车次将于 07/11 08:00 起售；已备好预填与候补建议",
  "action_url": "https://www.12306.cn/",       // 官方入口（不代购）
  "prefill": { /* 透传 DD-09 RailPrefill/FlightPrefill，供用户一键复制 */ },
  "ics": { "summary": "高铁起售", "dtstart": "2026-07-11T08:00:00+08:00",
           "duration_min": 15, "alarm_before_min": 0 },   // ICS/日历侧字段（§5.4）
  "disclaimer": "起售时间以 12306 当前页面为准",              // 估算类提醒必带
  "evidence": {"source_type": "rule", "verification_status": "estimated"}  // 时点来源
}
```

---

## 4. 接口契约

### 4.1 compose 节点与 bundle 组装（Python 函数签名）

```python
from __future__ import annotations
from wheretogo.schemas.evidence import Evidence, Fact       # DD-01/DD-03 复用
from wheretogo.enums import BundleVersion, VerificationStatus, SourceType

# —— DD-02 await_booking 期调用：探索版（交通确认前）——
def compose_explore_bundle(state: "TripPlanState") -> dict:
    """读 transport_options/candidate_cities/constraints/activities，
    组装探索版 payload（§3.3 explore 块）；出稿前跑闸三；写 trip_bundles(version='explore')。
    返回 payload dict（同时被 DD-02 塞入 interrupt payload 的 explore_bundle）。"""

# —— DD-02 compose 节点（最终闸）：确认版 ——
def plan_composer(state: "TripPlanState") -> dict:
    """DD-02 图节点入口。组装确认版 → 跑闸三最终闸 → 写 trip_bundles(version='confirm')
    → enqueue_reminders。返回 {"bundle": payload}（LangGraph LastValue 写 state['bundle']）。"""

def compose_confirm_bundle(state: "TripPlanState") -> dict:
    """组装确认版 payload（§3.3 confirm 块）。"""

# —— 最终闸：闸三断言 + 占位替换（DD-03 §6）——
def run_final_gate(payload: dict) -> tuple[dict, int]:
    """出稿前跑 assert_no_fabricated_transport；命中则替换占位，返回 (清洗后 payload, 违规计数)。
    违规计数进硬 KPI 埋点（DD-02 §12），期望恒为 0（源头 DD-09 已保证）。"""

# —— 落库 ——
def persist_bundle(plan_id: int, version: BundleVersion, payload: dict) -> int:
    """写 trip_bundles 一条不可变快照，返回 bundle_id。"""
```

### 4.2 提醒入队与投递（Python 函数签名）

```python
from datetime import datetime

def enqueue_reminders(plan_id: int, state: "TripPlanState") -> list[int]:
    """依 state 计算九类提醒的 fire_at 与 channel，写 reminders(status='scheduled')。
    presale 来自 state['transport_options']['presale']（DD-09）。返回 reminder_id 列表。"""

def build_presale_reminders(plan_id: int, transport_options: dict) -> list[dict]:
    """从 DD-09 transport_options.presale 逐条转 reminders.payload（type='presale'）。"""

def build_ics(plan_id: int) -> str:
    """实时读取 reminders + 确认版 timeline，生成 RFC5545 VCALENDAR 文本（零落盘）。"""

# —— Web Push（VAPID）——
def send_web_push(subscription: dict, payload: dict) -> bool:
    """向单个浏览器订阅端点投递（pywebpush + VAPID）。失败返回 False（worker 记 failed）。"""

# —— Celery beat 扫描到点提醒 ——
def dispatch_due_reminders(now: datetime | None = None) -> int:
    """Celery 周期任务：取 status='scheduled' 且 fire_at<=now 的提醒，按 channel 投递。返回投递条数。"""
```

### 4.3 BFF / HTTP API 契约（bundle DTO / ICS / Push）

| 方法 | 路径 | 说明 | 返回 |
|---|---|---|---|
| GET | `/plans/{id}/bundle?version=confirm` | 取最新版本 bundle 快照（默认取最新 confirm，无则 explore） | `200` bundle payload（§3.3） |
| GET | `/plans/{id}/bundle/versions` | 列出该 plan 的所有 bundle 快照（版本+时间） | `200` `[{version, created_at, id}]` |
| **GET** | **`/plans/{id}/calendar.ics`** | **ICS 动态订阅（零依赖，实时生成、零落盘）** | `200` `text/calendar`（§5.4） |
| POST | `/plans/{id}/push/subscribe` | 保存浏览器 Push 订阅（endpoint+keys） | `201` |
| DELETE | `/plans/{id}/push/subscribe` | 退订 | `204` |
| GET | `/push/vapid-public-key` | 前端注册 Push 所需 VAPID 公钥 | `200` `{key}` |
| GET | `/plans/{id}/reminders` | 列出提醒（供前端「将提醒你…」预览） | `200` `[reminders.payload 子集]` |

> **ICS 订阅铁律**：`Content-Type: text/calendar; charset=utf-8`，附 `Content-Disposition: attachment; filename="wheretogo-{id}.ics"`；URL 用**不可猜测令牌**（`/plans/{id}/calendar.ics?token=...`）保护，日历客户端周期性 GET 即自动刷新（动态订阅）。

### 4.4 SSE 渲染契约（对齐 DD-02 §11；前端据此渲染六态）

BFF 把 LangGraph 节点产物按 DD-02 §11 事件流推给前端；**前端只按 `event` 类型渲染卡片，字段一律经 FactField 读 `evidence.verification_status`**：

```jsonc
// 1) 逐节点产物（探索期城市卡/交通卡等；data 内每事实字段带 evidence）
{ "event": "node_output", "node": "discover",
  "data": { "candidate_cities": [ { "name": {"value":"北京","evidence":{...}}, ... } ] } }
{ "event": "node_output", "node": "transport",
  "data": { "transport_options": { "candidates": [ /* 门到门/三方案/策略卡 */ ] } } }

// 2) 中断：探索版 bundle + 预填 + 起售提醒（前端渲染探索版 + 待确认清单）
{ "event": "interrupt", "node": "await_booking",
  "data": { "type": "await_booking",
            "explore_bundle": { /* §3.3 explore 块，每字段带 evidence */ },
            "prefill": { "rail": {...}, "flight": {...} },
            "presale_reminders": [ {"train_window":"周六早","open_at":"...","evidence":{...}} ] } }

// 3) 完成：确认版 bundle（已过最终闸）
{ "event": "done", "node": "compose",
  "data": { "bundle": { /* §3.3 confirm 块，已过闸三 */ },
            "reminders_scheduled": 7 } }

// 4) 错误/降级（前端标注“降级”，不误当已确认）
{ "event": "error", "node": "compose", "data": { "message": "...", "degraded": true } }
```

> **前端渲染唯一入口**：所有事实字段（`{value, evidence}`）都必须经 `FactField` 组件渲染（§7.1），**禁止直接渲染 `value`**——这是 PRD 硬 KPI「未确认误展为已确认 = 0」的前端落地（DD-03 §7）。

---

## 5. 核心逻辑（可运行级代码）

> 时区统一 `Asia/Shanghai`；金额单位分（`INT`）；所有事实字段透传各领域产物的 `evidence`，**本节点不重造 confirmed 态**；出稿前**必过**闸三最终闸。

### 5.0 公共依赖与工具

```python
from __future__ import annotations
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any

from wheretogo.schemas.evidence import Evidence          # DD-01 §5 / DD-03
from wheretogo.enums import (BundleVersion, VerificationStatus, SourceType,
                             ReminderType, ReminderChannel)
# DD-03 闸三（证据护栏唯一入口，禁止绕过）
from dd03_guard import assert_no_fabricated_transport, iter_facts, ProvenanceError

CN_TZ = ZoneInfo("Asia/Shanghai")

def _field(value: Any, ev: dict | Evidence) -> dict:
    """把值+证据包装成 BundleField（§3.2）。ev 已是 DD-01 §5 JSONB 或 Evidence。"""
    evj = ev.to_jsonb() if isinstance(ev, Evidence) else ev
    return {"value": value, "evidence": evj}

def _now() -> str:
    return datetime.now(CN_TZ).isoformat()
```

### 5.1 探索版组装 `compose_explore_bundle`

```python
def compose_explore_bundle(state: dict) -> dict:
    """交通确认前的探索版：目的地/主题/推荐交通/时间窗/预算区间/核心活动/住宿区域/待确认清单。
    每字段带 evidence（透传各领域产物）。出稿前跑闸三，写 trip_bundles(version='explore')。"""
    cons = state["constraints"]
    topt = state.get("transport_options", {}) or {}
    cands = topt.get("candidates", [])
    top = cands[0] if cands else {}                          # discover 排序第一 = 顶选
    plan_id = _plan_id(state)

    # —— 目的地/推荐交通：透传 DD-08/DD-09（recommended_mode 属规则判断→estimated）——
    dest_ev = Evidence(source_type=SourceType.editorial,
                       verification_status=VerificationStatus.public_source_observed,
                       note="来自城市档案与候选发现")
    mode_ev = Evidence(source_type=SourceType.amap,   # 门到门=规则+高德综合，标 estimated
                       verification_status=VerificationStatus.estimated,
                       note="门到门比较为估算")

    explore = {
        "destination": _field(top.get("city"), dest_ev),
        "theme": _pick_theme(cons, state.get("activities", [])),
        "recommended_transport": {
            "mode": _field(top.get("recommended_mode", "compare"), mode_ev),
            "reason": top.get("reason", ""),
        },
        "depart_window": _field(_depart_window(topt), mode_ev),
        "return_window": _field(_return_window(topt), mode_ev),
        "budget_band": _field(_budget_band(cons), Evidence(
            source_type=SourceType.rule, verification_status=VerificationStatus.estimated,
            note="预算区间为约束聚合估算")),
        "core_activities": _core_activities(state.get("activities", [])),   # 透传 evidence
        "lodging_area": _field(_lodging_area(top), Evidence(
            source_type=SourceType.editorial,
            verification_status=VerificationStatus.public_source_observed,
            note="住宿区域建议（区域级，不含门牌）")),
        "transport_compare": top,                              # 透传门到门/三方案/策略卡
        "todo_checklist": _build_todo(cons, topt),
    }
    payload = {
        "schema_version": "bundle-1.0", "plan_id": str(plan_id), "version": "explore",
        "generated_at": _now(), "stage": state.get("stage", "await_booking"),
        "title": _title(state, top), "summary": top.get("reason", ""),
        "share": {"shareable": True, "desensitized": True},
        "explore": explore, "confirm": None,
        "reminders_preview": _preview_presale(topt),
        "disclaimer": "票价/余票/起售时间以官方平台当前页面为准；带“估算/待确认”标记的字段非最终值",
    }
    payload, _ = run_final_gate(payload)                       # 探索版同样过闸三（防上游污染）
    persist_bundle(plan_id, BundleVersion.explore, payload)
    return payload

def _core_activities(activities: list[dict]) -> list[dict]:
    """透传 DD-05/06 活动的 evidence，绝不新造 confirmed。取前 N 条核心活动。"""
    out = []
    for a in activities[:5]:
        ev = a.get("evidence") or {"source_type": "search",
                                   "verification_status": "unknown"}
        out.append({
            "title": _field(a.get("title"), ev),
            "venue": _field(a.get("venue"), ev),
            "start_at": _field(a.get("start_at"), ev),
            "price_text": _field(a.get("price_text"), ev),   # 原文价格，不换算
            "booking_url": _field(a.get("booking_url"), ev),
            "map": _map_links(a),                              # 静态图 + 高德深链（§7.4）
        })
    return out
```

### 5.2 确认版组装 `compose_confirm_bundle` 与 `plan_composer`（最终闸节点）

```python
def plan_composer(state: dict) -> dict:
    """DD-02 compose 节点入口（最终闸）：组装确认版 → 跑闸三 → 落库 → 入队提醒。"""
    payload = compose_confirm_bundle(state)
    payload, violations = run_final_gate(payload)              # ★ 最终闸（DD-03 §6）
    plan_id = _plan_id(state)
    persist_bundle(plan_id, BundleVersion.confirm, payload)
    emit_kpi("compose.transport_fabrication", violations)      # 硬 KPI，期望恒 0（DD-02 §12）
    enqueue_reminders(plan_id, state)                          # 写 reminders（§5.5）
    return {"bundle": payload}

def compose_confirm_bundle(state: dict) -> dict:
    plan_id = _plan_id(state)
    explore = compose_explore_bundle(state)["explore"]         # 概览沿用探索版块
    confirm = {
        "activities": _core_activities(state.get("activities", [])),
        "dining": [_dining_field(d) for d in state.get("dining", [])],
        "local_routes": [_route_field(r) for r in state.get("local_routes", [])],
        "timeline": _timeline_fields(state),                   # 消费 DD-12 timeline_slots（§8 降级）
        "confirmed_cost": _confirmed_cost(state.get("bookings", [])),   # 仅 confirmed_by_user
        "estimated_cost": _estimated_cost(state),              # estimated，与已确认视觉可辨
        "risks": _risks(state),                                # 天气/返程紧/未确认项
        "alternatives": _alternatives(state),
    }
    dest = explore["destination"]["value"]
    return {
        "schema_version": "bundle-1.0", "plan_id": str(plan_id), "version": "confirm",
        "generated_at": _now(), "stage": "confirm",
        "title": f"{dest} · 确认版计划" if dest else "确认版计划",
        "summary": "回填完成，已生成逐小时时间线与花费明细",
        "share": {"shareable": True, "desensitized": True},
        "explore": explore, "confirm": confirm,
        "reminders_preview": _preview_reminders(state),
        "disclaimer": "票价/余票以官方平台为准；预估待花为估算值(estimated)",
    }

def _confirmed_cost(bookings: list[dict]) -> dict:
    """已确认花费只能来自 bookings（confirmed_by_user）；无确认价则不计入（不臆测）。"""
    items, total = [], 0
    for b in bookings:
        if not b.get("confirmed"):
            continue
        amt = (b.get("extracted") or {}).get("price_cents")
        if amt is None:
            continue                                            # 用户未确认价格→不编造
        items.append({"label": _booking_label(b), "amount_cents": amt,
                      "evidence": b.get("evidence")})
        total += amt
    return {"items": items, "total_cents": total}

def _plan_id(state: dict) -> int:
    return int(str(state["plan_id"]).split(":")[-1])
```

### 5.3 最终闸 `run_final_gate`（调用 DD-03 闸三 + 占位替换）

```python
FORBIDDEN_PLACEHOLDER = "待你在官方平台确认"

def run_final_gate(payload: dict) -> tuple[dict, int]:
    """出稿前跑 DD-03 §6 assert_no_fabricated_transport。
    命中（train.*/flight.*/*.availability 来源=llm）→ 替换占位并计数，不整链失败。"""
    violations = 0
    try:
        assert_no_fabricated_transport(payload)                # DD-03：命中即 raise
    except ProvenanceError:
        pass                                                    # 交给下方逐字段替换（兜底出稿）
    for field_name, fact in iter_facts(payload):
        src = (fact.get("evidence") or {}).get("source_type")
        is_transport = field_name.startswith(("train.", "flight.")) or field_name.endswith(".availability")
        if is_transport and src == "llm":
            fact["value"] = FORBIDDEN_PLACEHOLDER
            fact["evidence"] = {
                "source_type": src, "source_url": None, "fetched_at": None,
                "verification_status": VerificationStatus.unknown.value,
                "confidence": 0.0,
                "note": "闸三替换：交通事实禁止 LLM 生成，请在官方平台确认",
            }
            violations += 1
    # 二次断言：替换后必须干净（进 CI 红线，硬 KPI=0）
    assert_no_fabricated_transport(payload)
    return payload, violations
```

> **语义**：DD-09 源头已保证交通事实恒非 `llm`，故 `violations` 期望恒为 0。本闸是**最终兜底**：万一上游/重规划注入了 `llm` 交通事实，这里**替换而非崩溃**（保证用户仍拿到 bundle），但违规计数报警（DD-02 §12），CI 用红线用例断言替换后 `assert_no_fabricated_transport` 通过。

### 5.4 ICS 动态订阅生成 `build_ics`（RFC5545，零依赖、零落盘）

```python
def _ics_dt(dt: datetime) -> str:
    """RFC5545 UTC 时间戳：20260711T000000Z。"""
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")

def _ics_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace("\n", r"\n")

def build_ics(plan_id: int) -> str:
    """实时读 reminders（+ 确认版 timeline）生成 VCALENDAR。日历客户端周期 GET 即自动刷新。"""
    reminders = fetch_reminders(plan_id)                       # DB 读 reminders
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//WhereToGo//TripBundle//CN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:周末去哪儿·计划{plan_id}"]
    for r in reminders:
        p = r["payload"]; ics = p.get("ics", {})
        dtstart = datetime.fromisoformat(ics.get("dtstart") or r["fire_at"].isoformat())
        dur = int(ics.get("duration_min", 15))
        uid = f"{r['id']}-{plan_id}@wheretogo"
        lines += [
            "BEGIN:VEVENT", f"UID:{uid}",
            f"DTSTAMP:{_ics_dt(datetime.now(CN_TZ))}",
            f"DTSTART:{_ics_dt(dtstart)}",
            f"DTEND:{_ics_dt(dtstart + timedelta(minutes=dur))}",
            f"SUMMARY:{_ics_escape(ics.get('summary') or p.get('title'))}",
            f"DESCRIPTION:{_ics_escape((p.get('body') or '') + (('  ' + p['disclaimer']) if p.get('disclaimer') else ''))}",
        ]
        if p.get("action_url"):
            lines.append(f"URL:{p['action_url']}")
        alarm = int(ics.get("alarm_before_min", 0))            # VALARM 提前提醒
        if alarm > 0:
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY",
                      f"TRIGGER:-PT{alarm}M",
                      f"DESCRIPTION:{_ics_escape(p.get('title'))}", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"                         # RFC5545 要求 CRLF
```

BFF 路由（FastAPI 示例，动态订阅、令牌保护、零落盘）：

```python
from fastapi import APIRouter, Response, HTTPException
router = APIRouter()

@router.get("/plans/{plan_id}/calendar.ics")
def get_calendar_ics(plan_id: int, token: str):
    if not verify_ics_token(plan_id, token):                   # 不可猜测令牌
        raise HTTPException(403)
    try:
        body = build_ics(plan_id)                              # 实时生成
    except Exception:
        body = build_ics_fallback(plan_id)                     # §8 ICS 兜底
    return Response(content=body, media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="wheretogo-{plan_id}.ics"',
                             "Cache-Control": "no-cache"})
```

### 5.5 提醒入队 `enqueue_reminders` + 起售提醒 `build_presale_reminders`

```python
def enqueue_reminders(plan_id: int, state: dict) -> list[int]:
    """依 state 计算九类提醒并写 reminders(status='scheduled')。channel 默认三通道各一行
    （投递时按订阅决定）；presale 来自 DD-09 transport_options.presale。"""
    cons = state["constraints"]
    topt = state.get("transport_options", {}) or {}
    trip_start = datetime.fromisoformat(cons["earliest_depart"])
    trip_end = datetime.fromisoformat(cons["latest_return"])
    payloads: list[dict] = []

    # ① 起售提醒（DD-09 presale）
    payloads += build_presale_reminders(plan_id, topt)
    # ② 活动预约开放（活动带 booking_open_at 时）
    for a in state.get("activities", []):
        if a.get("booking_open_at"):
            payloads.append(_reminder(ReminderType.activity_booking, a["booking_open_at"],
                                      f"{a.get('title')} 预约开放", a.get("booking_url"),
                                      a.get("evidence")))
    # ③ 机票复查（行前 5 天，仅当选飞机）
    if cons.get("accept_flight", True):
        payloads.append(_reminder(ReminderType.flight_recheck,
                                  (trip_start - timedelta(days=5)).isoformat(),
                                  "机票复查：确认航班时刻/航站楼/退改", None,
                                  {"source_type": "rule", "verification_status": "estimated"}))
    # ④ 行前 72h 确认 / ⑤ 行前 24h 天气
    payloads.append(_reminder(ReminderType.pre_trip_72h,
                              (trip_start - timedelta(hours=72)).isoformat(),
                              "行前 72h：确认交通/住宿/活动是否有变", None,
                              {"source_type": "rule", "verification_status": "estimated"}))
    payloads.append(_reminder(ReminderType.weather_24h,
                              (trip_start - timedelta(hours=24)).isoformat(),
                              "行前 24h：天气检查，必要时启用室内备选", None,
                              {"source_type": "rule", "verification_status": "estimated"}))
    # ⑥ 证件检查（行前 48h）
    payloads.append(_reminder(ReminderType.doc_check,
                              (trip_start - timedelta(hours=48)).isoformat(),
                              "证件检查：身份证/购票证件随身", None,
                              {"source_type": "rule", "verification_status": "estimated"}))
    # ⑦ 酒店免费取消截止（有 booking 且带 cancel_deadline）
    for b in state.get("bookings", []):
        cd = (b.get("extracted") or {}).get("cancel_deadline")
        if b.get("kind") == "hotel" and cd:
            payloads.append(_reminder(ReminderType.hotel_cancel_deadline, cd,
                                      "酒店免费取消截止", None, b.get("evidence")))
    # ⑧ 活动开场（确认版 timeline 的 activity 槽，提前 60min）/ ⑨ 返程提醒（提前 120min）
    for slot in _iter_activity_slots(state):
        payloads.append(_reminder(ReminderType.activity_start, slot["start_at"],
                                  f"活动开场：{slot['title']}", None, slot.get("evidence"),
                                  alarm_before_min=60))
    payloads.append(_reminder(ReminderType.return_trip, trip_end.isoformat(),
                              "返程提醒：预留进站/值机缓冲", None,
                              {"source_type": "rule", "verification_status": "estimated"},
                              alarm_before_min=120))

    return persist_reminders(plan_id, _fan_out_channels(payloads))

def build_presale_reminders(plan_id: int, transport_options: dict) -> list[dict]:
    """透传 DD-09 transport_options.presale（每项含 open_at + evidence=rule/estimated）。"""
    out = []
    for p in transport_options.get("presale", []):
        out.append({
            "type": ReminderType.presale.value,
            "title": f"{p.get('route','')} 高铁起售提醒",
            "body": f"{p.get('train_window','')} 车次将于 {p.get('open_at')} 起售；已备好预填与候补建议",
            "action_url": "https://www.12306.cn/",             # 官方入口（不代购）
            "prefill": transport_options.get("prefill", {}).get("rail", {}),
            "fire_at": p["open_at"],                            # 起售时点即 fire_at
            "ics": {"summary": "高铁起售", "dtstart": p["open_at"], "duration_min": 15,
                    "alarm_before_min": 0},
            "disclaimer": p.get("disclaimer", "起售时间以 12306 当前页面为准"),
            "evidence": p.get("evidence", {"source_type": "rule",
                                           "verification_status": "estimated"}),
        })
    return out

def _reminder(rtype: ReminderType, fire_at: str, title: str, url: str | None,
              evidence: dict | None, *, body: str = "", alarm_before_min: int = 0) -> dict:
    return {"type": rtype.value, "title": title, "body": body or title, "action_url": url,
            "fire_at": fire_at,
            "ics": {"summary": title, "dtstart": fire_at, "duration_min": 15,
                    "alarm_before_min": alarm_before_min},
            "disclaimer": "以官方平台当前页面为准" if rtype in (
                ReminderType.presale, ReminderType.flight_recheck) else None,
            "evidence": evidence or {"source_type": "rule", "verification_status": "estimated"}}

def _fan_out_channels(payloads: list[dict]) -> list[dict]:
    """每条提醒默认落三通道各一行（web_push/email/ics）；用户订阅决定实际投递（§5.6）。"""
    rows = []
    for p in payloads:
        for ch in (ReminderChannel.web_push, ReminderChannel.email, ReminderChannel.ics):
            rows.append({"type": p["type"], "fire_at": p["fire_at"],
                         "channel": ch.value, "payload": p, "status": "scheduled"})
    return rows
```

### 5.6 Web Push（VAPID）投递 + Celery beat 到点扫描

```python
from pywebpush import webpush, WebPushException

def send_web_push(subscription: dict, payload: dict) -> bool:
    try:
        webpush(subscription_info=subscription,
                data=json.dumps({"title": payload["title"], "body": payload["body"],
                                 "url": payload.get("action_url")}, ensure_ascii=False),
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.VAPID_SUBJECT})
        return True
    except WebPushException:
        return False                                            # worker 记 failed，不阻塞其它通道

# —— Celery beat 周期任务：扫描到点提醒并投递 ——
from celery import shared_task

@shared_task
def dispatch_due_reminders(now: str | None = None) -> int:
    ts = datetime.fromisoformat(now) if now else datetime.now(CN_TZ)
    due = fetch_scheduled_reminders_due(ts)                     # status='scheduled' & fire_at<=now
    sent = 0
    for r in due:
        ch, p = r["channel"], r["payload"]
        ok = True
        if ch == "web_push":
            subs = fetch_push_subscriptions(r["plan_id"])
            ok = any(send_web_push(s, p) for s in subs) if subs else False
        elif ch == "email":
            ok = send_email(r["plan_id"], p)                    # SES / Resend（§10）
        elif ch == "ics":
            continue                                            # ICS 为订阅拉取，不主动推；跳过
        mark_reminder(r["id"], "sent" if ok else "failed")
        sent += int(ok)
    return sent
```

Celery beat 配置（每分钟扫描一次）：

```python
CELERYBEAT_SCHEDULE = {
    "dispatch-due-reminders": {
        "task": "wheretogo.reminders.dispatch_due_reminders",
        "schedule": 60.0,                                       # 每 60s 扫描到点提醒
    }
}
```

---

## 6. 与其他模块接线（环环相扣）

本节点是全链路**最后一站**，只**消费**上游产物并做最终校验，不回写上游事实。接线矩阵如下：

| 上游/协作模块 | 本节点消费的字段 | 消费方式 | 契约要点 |
|---|---|---|---|
| **DD-03 证据护栏** | `assert_no_fabricated_transport` / `iter_facts` / `ProvenanceError` | **出稿前调用（最终闸）**；命中→占位替换 | 唯一入口，禁止绕过；替换后**再断言一次**必须通过（§5.3） |
| **DD-02 编排层** | `TripPlanState`（全部产物）、`stage` | `plan_composer` 为 `compose` 节点入口；`compose_explore_bundle` 供 `await_booking` interrupt | 写 `state['bundle']`（LastValue）；SSE 事件按 DD-02 §11 |
| **DD-09 交通** | `transport_options.candidates[*]`、`transport_options.presale`、`prefill` | 透传 `transport_compare`；`build_presale_reminders` 逐条转起售提醒 | 交通事实 evidence **原样透传**，本节点绝不新造 |
| **DD-12 时间线** | `timeline_slots` | `_timeline_fields` 透传；**不存在时**走 §8 规则降级 | slot 的 `start_at/end_at` 带 evidence |
| **DD-05 检索 / DD-06 活动 / DD-08 城市 / DD-11 住宿餐饮** | `activities`、`candidate_cities`、`dining`、`local_routes`、`hotel_area` | `_core_activities`/`_dining_field`/`_route_field` **透传 evidence** | 只取值+证据组装，定级归上游 |
| **DD-10 回填** | `bookings[*].confirmed`、`bookings[*].extracted.price_cents` | `_confirmed_cost` 只计 `confirmed_by_user` 项 | 无确认价→不计入、不臆测 |
| **DD-01 存储层** | `trip_bundles` / `reminders` 两表 | `persist_bundle` / `enqueue_reminders` 写入 | payload 内 evidence 必过 `Evidence.to_jsonb()` |
| **BFF / 前端** | bundle payload、SSE 事件 | 前端所有事实字段经 `FactField` 渲染（§7.1） | 「未确认误展=0」前端落地点 |

> **接线自检清单（联调必过）**：① `compose` 出稿前必调闸三且替换后复断言通过；② `presale` 条目数 == 起售提醒条数；③ `confirmed_cost` 仅含 `confirmed_by_user`；④ bundle 每事实字段可被 `FactField` 命中六态；⑤ DD-12 缺失时降级时间线仍能渲染计划卡。

---

## 7. 证据与状态（最终闸 · 六态渲染 · 分享脱敏）

### 7.1 FactField 六态渲染（**严格照 DD-03 §7，前端唯一入口**）

所有 `{value, evidence}` 字段一律经 `FactField` 渲染，据 `evidence.verification_status` 映射为**一眼可辨**的六态。**这是 PRD 硬 KPI「未确认误展为已确认 = 0」的前端落地**，不得直接渲染 `value`。

| verification_status | 徽标文案 | 视觉（克制，无炫技） | 语义 |
|---|---|---|---|
| `confirmed_by_user` | ✅ 你已确认 | 绿底实心徽标 | 用户回填确认（最高可信） |
| `official_source_confirmed` | 🔷 官方确认 | 蓝底实心徽标 | 官方源核实 |
| `public_source_observed` | 🌐 公开可查 | 灰蓝描边徽标 | 公开来源观测 |
| `estimated` | ≈ 估算 | 黄底描边 + 斜体值 | 规则/模型估算，**非最终值** |
| `unknown` | ⚪ 待确认 | 虚线描边 + 「请到官方平台确认」占位 | 无可信来源/闸三替换 |
| `expired` | ⌛ 可能过期 | 灰底删除线 + `fetched_at` | 证据超时效窗 |

```tsx
// FactField.tsx —— 克制实现：只做状态映射与徽标，无动画/无交互
import type { ReactNode } from "react";

const STATUS_META: Record<string, {label: string; cls: string}> = {
  confirmed_by_user:         { label: "你已确认", cls: "ff-confirmed" },
  official_source_confirmed: { label: "官方确认", cls: "ff-official" },
  public_source_observed:    { label: "公开可查", cls: "ff-public" },
  estimated:                 { label: "估算",     cls: "ff-estimated" },
  unknown:                   { label: "待确认",   cls: "ff-unknown" },
  expired:                   { label: "可能过期", cls: "ff-expired" },
};

export function FactField({ field, render }:
    { field: {value: unknown; evidence?: {verification_status: string; source_url?: string; fetched_at?: string}};
      render?: (v: unknown) => ReactNode }) {
  const st = field.evidence?.verification_status ?? "unknown";
  const meta = STATUS_META[st] ?? STATUS_META.unknown;
  const isUnknown = st === "unknown" || field.value == null;
  return (
    <span className={`factfield ${meta.cls}`}>
      {isUnknown ? <em className="ff-placeholder">请到官方平台确认</em>
                 : <span className="ff-value">{render ? render(field.value) : String(field.value)}</span>}
      <sup className="ff-badge" title={field.evidence?.source_url ?? ""}>{meta.label}</sup>
      {st === "expired" && field.evidence?.fetched_at &&
        <small className="ff-fetched">（{field.evidence.fetched_at} 抓取）</small>}
    </span>
  );
}
```

### 7.2 结构化卡片（静态渲染，无交互）

三类卡片全部为**只读静态渲染**，字段经 `FactField`：

- **城市卡（CityCard）**：目的地名/主题/推荐理由 + 静态地图缩略图（§7.4）。
- **交通卡（TransportCard）**：门到门比较（高铁/飞机/混合三方案）、`recommended_mode`、`depart/return_window`；**票价/余票字段若为 `unknown`（含闸三替换）→ 显示「请到官方平台确认」+ 高德/12306 深链**。
- **计划卡（PlanCard）**：确认版逐小时 timeline（transport/activity/meal/lodging/buffer）、`confirmed_cost` vs `estimated_cost` **分区展示**（已确认绿区 / 估算黄区，视觉可辨）、risks、alternatives。

### 7.3 分享卡脱敏（默认隐藏隐私字段）

```python
SHARE_HIDDEN_KEYS = {"exact_address", "door_number", "personal_budget",
                     "id_number", "passport", "private_note", "phone"}

def desensitize_for_share(payload: dict) -> dict:
    """分享卡默认脱敏：隐藏精确地址/个人预算/证件/私人备注/电话。
    住宿仅保留区域级（lodging_area），删除门牌与个人预算带。"""
    import copy
    p = copy.deepcopy(payload)
    p.setdefault("share", {})["desensitized"] = True

    def scrub(node):
        if isinstance(node, dict):
            for k in list(node.keys()):
                if k in SHARE_HIDDEN_KEYS:
                    node.pop(k, None)
                else:
                    scrub(node[k])
        elif isinstance(node, list):
            for it in node:
                scrub(it)

    scrub(p)
    # 个人预算区间只在私有视图显示；分享卡移除 budget_band 的精确数值
    exp = p.get("explore") or {}
    if "budget_band" in exp:
        exp["budget_band"] = {"value": "（分享卡已隐藏个人预算）",
                              "evidence": {"source_type": "rule",
                                           "verification_status": "unknown"}}
    return p
```

### 7.4 地图（克制：静态图 / 高德深链，**不做双栏交互地图**）

```python
def _map_links(entity: dict) -> dict:
    """生成静态地图缩略图 URL + 高德一键直达深链（uri.amap.com）。
    本阶段不嵌交互地图；点击深链在高德打开。"""
    lng, lat = entity.get("lng"), entity.get("lat")
    name = entity.get("venue") or entity.get("name") or ""
    static_img = (f"https://restapi.amap.com/v3/staticmap?location={lng},{lat}"
                  f"&zoom=15&size=400*200&markers=mid,,A:{lng},{lat}"
                  f"&key={{AMAP_STATIC_KEY}}") if lng and lat else None
    amap_uri = (f"https://uri.amap.com/marker?position={lng},{lat}&name={name}"
                if lng and lat else None)
    return {"static_img_url": static_img, "amap_url": amap_uri}
```

### 7.5 证据定级铁律（本节点自守）

1. **不升级**：透传字段 evidence 保持上游原值，本节点**绝不**把 `estimated/unknown` 改写为 `confirmed`。
2. **闸三降级唯一例外**：交通 `llm` 事实 → 强制降为 `unknown` 占位（§5.3）。
3. **已确认花费来源单一**：`confirmed_cost` 只接 `bookings.confirmed_by_user`（DD-10），其余一律入 `estimated_cost`。
4. **估算必带免责**：任何 `estimated` 时点/金额，卡片与提醒都附「以官方平台当前页面为准」。

---

## 8. 降级设计（AI 异常 / DD-12 缺失 / ICS 兜底）

| 触发场景 | 降级策略 | 产出可信度标记 |
|---|---|---|
| **compose LLM 摘要/主题异常** | 用规则模板拼 `title/summary/theme`（城市名+主题词+方案），不阻断出稿 | 结构性文案（非事实字段），无 evidence |
| **闸三命中交通 `llm` 事实** | 替换「待你在官方平台确认」占位（§5.3），不崩溃 | `unknown` + `note=闸三替换` |
| **DD-12 `timeline_slots` 不存在/为空** | `_timeline_fields` 用 `bookings + activities + dining + route_legs` 按 `start_at` 排序规则拼装降级时间线 | slot evidence 透传源字段；缺失时点标 `estimated` |
| **各领域产物缺 evidence** | 兜底 `Evidence.unknown()`（`source_type=llm, status=unknown`） | `unknown`，前端显「待确认」 |
| **enqueue_reminders 某类计算失败** | 单类 try/except 跳过并记 warning，不影响其它提醒入队 | 该类不入队；日志告警 |
| **Web Push 投递失败** | `mark_reminder(failed)`；ICS/邮件通道不受影响 | 不重试阻塞；下轮 beat 不再取 |
| **ICS 生成异常** | `build_ics_fallback` 返回**仅含返程/行前 72h 的最小 VCALENDAR**，保证订阅链接始终 200 | 最小可用日历 |

```python
def _timeline_fields(state: dict) -> list[dict]:
    """优先消费 DD-12 timeline_slots；不存在则规则降级拼装。"""
    slots = state.get("timeline_slots")
    if slots:
        return [_slot_field(s) for s in slots]                  # 透传 DD-12
    # —— 降级：bookings+activities+dining+route_legs 按 start_at 排序 ——
    events = []
    for b in state.get("bookings", []):
        events.append((_get(b, "depart_at"), "transport", _booking_label(b), b))
    for a in state.get("activities", []):
        events.append((_get(a, "start_at"), "activity", a.get("title"), a))
    for d in state.get("dining", []):
        events.append((_get(d, "open_at"), "meal", d.get("name"), d))
    events = [e for e in events if e[0]]
    events.sort(key=lambda e: e[0])
    out = []
    for i, (t, kind, title, raw) in enumerate(events, 1):
        ev = raw.get("evidence") or Evidence.estimated(note="降级时间线").to_jsonb()
        out.append({"seq": i, "start_at": _field(t, ev), "kind": kind, "title": title})
    return out

def build_ics_fallback(plan_id: int) -> str:
    """ICS 兜底：仅含行前 72h + 返程两条 VEVENT，保证订阅链接恒 200。"""
    cal = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//WhereToGo//DD13//CN"]
    for r in fetch_min_reminders(plan_id, types=("pre_trip_72h", "return_trip")):
        cal += _vevent(r)
    cal.append("END:VCALENDAR")
    return "\r\n".join(cal)
```

---

## 9. 前端克制清单（必做 vs 明确延后，引用 v1.1 增补 B）

> **铁律（v1.1 增补 B）**：本阶段前端 **P0 只交付「证据六态可辨 + 结构化卡片静态渲染」**；一切富交互/富媒体一律延后。前端不酒炫、不抢戏。

### 9.1 ✅ 必做（P0）

| 项 | 说明 | 验收锂 |
|---|---|---|
| **FactField 六态渲染** | 所有事实字段经 `FactField`（§7.1），六态一眼可辨 | 未确认与已确认视觉区分百分百 |
| **城市卡/交通卡/计划卡静态渲染** | 只读卡片，无交互（§7.2） | 结构化字段完整展示 |
| **已确认/估算花费分区** | `confirmed_cost` 绿区 / `estimated_cost` 黄区 | 两区视觉可辨 |
| **待确认清单** | 探索版 `todo_checklist` 静态列表 | 逐项可见（不做拖拽勾选交互） |
| **地图 = 静态图/高德深链** | 缩略图 + 一键直达（§7.4） | 点击在高德打开 |
| **分享卡（脱敏）** | 默认隐藏隐私字段（§7.3） | 分享无精确地址/个人预算/证件 |
| **SSE 流渲染** | 按 `node_output`/`interrupt`/`done`/`error` 逐节点渲染（§4.4） | 降级事件标「降级」不误展 |
| **提醒预览「将提醒你…」** | 静态展示 `reminders_preview` | 可读摘要列表 |

### 9.2 ❌ 明确延后（本阶段不做）

- ❌ **预算滑杆**（拖动调价）—— 延后；本阶段仅静态展示 `budget_band`。
- ❌ **双栏交互地图**（地图联动卡片）—— 延后；用静态图 + 高德深链代替。
- ❌ **拖拽编辑行程**（时间线拖拽重排）—— 延后；时间线仅只读。
- ❌ **手绘图/富媒体插图** —— 延后；不引入装饰性插画。
- ❌ **实时协作/多人编辑 UI** —— 延后（约束聚合在 DD-07 后端）。
- ❌ **任何下单/支付/账户 UI** —— 永不做（硬约束①不做交易）。

---

## 10. 配置（VAPID / 邮件 / ICS）

```python
# wheretogo/config.py（pydantic-settings）—— 新增本节点相关项
class Settings(BaseSettings):
    # —— Web Push (VAPID) ——
    VAPID_PUBLIC_KEY: str                       # 前端注册用（GET /push/vapid-public-key）
    VAPID_PRIVATE_KEY: str                      # 服务端签名（勿入仓）
    VAPID_SUBJECT: str = "mailto:ops@wheretogo.app"
    # —— 邮件（SES / Resend 二选一）——
    EMAIL_PROVIDER: str = "resend"              # resend | ses
    RESEND_API_KEY: str | None = None
    AWS_SES_REGION: str | None = None
    EMAIL_FROM: str = "WhereToGo <no-reply@wheretogo.app>"
    # —— ICS 动态订阅 ——
    ICS_TOKEN_SECRET: str                       # 签发不可猜测订阅令牌
    ICS_CALENDAR_NAME: str = "周末去哪儿"
    # —— 静态地图 ——
    AMAP_STATIC_KEY: str | None = None          # 高德静态图 key
```

| 配置 | 用途 | 备注 |
|---|---|---|
| `VAPID_*` | Web Push 签名与前端订阅 | 公钥下发前端，私钥仅服务端 |
| `EMAIL_PROVIDER` + key | 邮件提醒通道 | Resend 零依赖接入；SES 需 IAM |
| `ICS_TOKEN_SECRET` | ICS URL 令牌签发/校验 | `/plans/{id}/calendar.ics?token=...` |
| `AMAP_STATIC_KEY` | 静态地图缩略图 | 无 key 时 `_map_links` 降级仅出高德深链 |

---

## 11. 效果与验收（含测试）

### 11.1 验收标准

| 项 | 验收标准 | 度量方式 |
|---|---|---|
| **未确认误展 = 0（硬 KPI）** | compose 产出的 bundle 中，`train.*`/`flight.*`/`*.availability` 无任何 `source_type=llm` 且 status 非 uncertain 的字段 | **CI 门禁**：`run_final_gate` 后再断言必过；`compose.transport_fabrication` 埋点恒 0 |
| **证据六态可辨** | 六种 status 均有区分样式，unknown 显占位 | 组件快照测试 + 人工目检 |
| **ICS 可订阅** | `GET /plans/{id}/calendar.ics` 返回合法 RFC5545，主流日历（Google/Apple/Outlook）可订阅刷新 | ICS 解析器校验 + 真实客户端订阅 |
| **可分享** | 分享卡无精确地址/个人预算/证件/私人备注 | `desensitize_for_share` 单测 |
| **版本快照** | 探索版/确认版各一条不可变 `trip_bundles` | 重规划产新快照，旧快照保留 |
| **提醒入队** | 9 类提醒 fire_at/channel 正确；presale 数 == DD-09 presale 条数 | 单测 + 集成测 |

### 11.2 关键测试用例

```python
def test_final_gate_replaces_llm_transport():
    """闸三最终闸：交通 llm 事实被替换为 unknown 占位，且复断言通过。"""
    payload = {"confirm": {"timeline": [
        {"seq": 1, "kind": "transport",
         "train.no": {"value": "G101",
                      "evidence": {"source_type": "llm",
                                   "verification_status": "estimated"}}}]}}
    cleaned, violations = run_final_gate(payload)
    fld = cleaned["confirm"]["timeline"][0]["train.no"]
    assert violations == 1
    assert fld["value"] == "待你在官方平台确认"
    assert fld["evidence"]["verification_status"] == "unknown"
    assert_no_fabricated_transport(cleaned)          # 不再抛错

def test_confirmed_cost_only_user_confirmed():
    """已确认花费只含 confirmed_by_user 且有价的 booking。"""
    bookings = [{"confirmed": True,  "extracted": {"price_cents": 112400}},
                {"confirmed": False, "extracted": {"price_cents": 50000}},
                {"confirmed": True,  "extracted": {}}]
    cost = _confirmed_cost(bookings)
    assert cost["total_cents"] == 112400 and len(cost["items"]) == 1

def test_presale_reminder_count_matches_dd09():
    """起售提醒条数 == DD-09 presale 条数。"""
    topt = {"presale": [{"train_window": "周六早", "open_at": "2026-07-11T08:00:00+08:00",
                         "evidence": {"source_type": "rule", "verification_status": "estimated"}}]}
    rs = build_presale_reminders(1024, topt)
    assert len(rs) == len(topt["presale"]) == 1
    assert rs[0]["type"] == "presale" and "12306" in rs[0]["action_url"]

def test_ics_is_valid_rfc5545():
    """ICS 首尾合规、含 VEVENT、CRLF 换行。"""
    ics = build_ics(1024)
    assert ics.startswith("BEGIN:VCALENDAR") and ics.rstrip().endswith("END:VCALENDAR")
    assert "BEGIN:VEVENT" in ics and "\r\n" in ics

def test_share_card_desensitized():
    """分享卡隐藏隐私字段。"""
    p = {"explore": {"budget_band": {"value": {"min": 1800}}, "exact_address": "XX路1号"}}
    out = desensitize_for_share(p)
    assert "exact_address" not in out["explore"]
    assert "隐藏" in out["explore"]["budget_band"]["value"]
```

> **CI 门禁（硬 KPI）**：`test_final_gate_replaces_llm_transport` 与「全量 bundle 扫描无交通 llm 事实」列为必过用例；任一失败即阻断发布。

---

## 12. 任务拆解

| 序 | 任务 | 产出 | 依赖 |
|---|---|---|---|
| T1 | `_field`/`_now` 等公共工具 + `BundleField` 封装 | `bundle/composer.py` 骨架 | DD-01/DD-03 schema |
| T2 | `compose_explore_bundle` 探索版组装 | 探索版 payload | DD-08/09 产物 |
| T3 | `compose_confirm_bundle` + `_confirmed_cost`/`_estimated_cost`/`_risks` | 确认版 payload | DD-10/11/12 产物 |
| T4 | `run_final_gate` 最终闸 + 占位替换 + KPI 埋点 | 闸三落地 | DD-03 `assert_no_fabricated_transport` |
| T5 | `persist_bundle` 写 `trip_bundles` 快照 | 版本快照 | DD-01 §8.6 |
| T6 | `_timeline_fields`（含 DD-12 缺失降级） | 时间线块 | DD-12（可选） |
| T7 | `build_presale_reminders` + `enqueue_reminders`（9 类） | 写 `reminders` | DD-09 presale |
| T8 | `build_ics` + `build_ics_fallback` + ICS 路由 | `GET /calendar.ics` | reminders 落库 |
| T9 | `send_web_push` + `dispatch_due_reminders` + Celery beat | 投递 worker | VAPID 配置 |
| T10 | BFF：bundle DTO / push 订阅 / vapid-public-key 路由 | HTTP API | T5/T9 |
| T11 | 前端 `FactField` + 城市/交通/计划卡 + 分享卡脱敏 | 克制 UI | SSE 契约 §4.4 |
| T12 | 单测 + CI 门禁（未确认误展=0） | 测试套件 | T4/T7/T8 |

---

## 13. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| **上游污染绕过闸三** | 交通 llm 事实误展为已确认（触硬 KPI） | 最终闸**必跑**且替换后**复断言**；CI 全量扫描门禁 |
| **DD-12 未就绪** | 无逐小时时间线 | §8 规则降级拼装；时点标 `estimated`，不阻断出稿 |
| **presale 时点估算偏差** | 用户误信起售时刻 | 提醒必带「以 12306 当前页面为准」；evidence 标 `estimated` |
| **ICS 令牌泄露** | 他人订阅到私有行程 | 不可猜测令牌 + 可吊销；分享卡默认脱敏 |
| **Web Push 送达率低** | 提醒漏达 | ICS 订阅为主通道（客户端主动拉取）；邮件兜底 |
| **快照膨胀** | `trip_bundles` 行数随重规划增长 | 只存渲染就绪 payload；按 plan 保留最近 N 版，历史归档 |
| **前端越界做富交互** | 违背 v1.1 克制约束、拖慢交付 | §9「明确延后」清单纳入 CR 检查项 |

---

> **本文与既有 DD 对齐声明**：`compose`=最终闸（DD-02 §5 / DD-03 §6）；六态渲染严格照 DD-03 §7；`trip_bundles`/`reminders` 结构引用 DD-01 §8.6/§8.7；presale 消费 DD-09 §3.2；timeline 消费 DD-12（缺失降级）；前端克制遵循 v1.1 增补 B。本节点只**透传+组装+最终校验**，不重造任何上游事实。

---

## v2 增补：聊天界面（撤销 v1.1 对聊天框的克制，对齐 DD-15）

- **撤销**：v1.1 增补 B 中“❌ 聊天框”一条**作废**——v2 **聊天框为必做**。
- **新增前端**：① 对话消息流（`assistant_delta`/`clarify` 渲染为气泡）；② 输入框（自然语言、多轮）；③ **卡片内嵌**（`node_output`/`interrupt`/`done` 渲染为城市卡/交通卡/计划卡）；④ 深搜进度（`research_progress` 流式“正在核实官方来源…”）。
- **不变的硬要求**：所有事实字段仍经 `FactField` 六态渲染（DD-03 §7）；对话正文**不出编造事实**（DD-15 §9，事实只在卡片里）。
- **仍延后**：预算滑杆 / 双栏交互地图 / 拖拽编辑（富交互，不影响“对话+实时+记忆”三项核心）。
- **SSE 事件契约**对齐 DD-15 §7 与 DD-02 §11/§16。
