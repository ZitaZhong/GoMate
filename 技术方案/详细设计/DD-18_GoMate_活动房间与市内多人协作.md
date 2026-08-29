# DD-18 GoMate 活动房间与市内多人协作 · 详细设计

**详细设计系列 · 市内模式新增能力 · v1.0 · 2026 年 7 月**

> 本文定义 GoMate 市内多人周末活动模式的核心领域能力：**活动房间状态机**、**多人信息收集与偏好聚合**、**主题选择系统**、**通勤公平性算法**、**集合点/时间计算**、**多人独立路线规划**、**AI 自然语言修改**与**行程版本管理**。
>
> **与跨城模式的关系**：本模块是市内模式的专用编排子图，与现有 DD-02 TripPlan 状态机**并存**。两种模式共享底层基础设施（DD-03 证据护栏、DD-04 Provider、DD-05 检索、DD-06 情报流水线、DD-16 记忆、DD-17 实时深研），但编排逻辑和前端视图独立。
>
> **上游依据**：GoMate PRD §5-§8；DD-02（LangGraph 状态机范式）；DD-04（AMap Provider）；DD-07（约束收集/聚合）；DD-11（市内交通）；DD-17（实时深研）。
> **下游消费者**：DD-19 前端、BFF 路由、DD-05 市内检索、DD-17 市内深研。

---

## 1. 模块职责与边界

| 项 | 说明 |
|---|---|
| **职责** | ① 管理活动房间生命周期（状态机）；② 收集多人约束并聚合（偏好冲突检测）；③ 主题选择编排（投票/转盘/AI/直选）；④ 计算通勤公平性并排序活动候选；⑤ 计算集合点/时间；⑥ 为每位成员生成独立路线；⑦ 编排 AI 自然语言修改（局部更新）；⑧ 行程版本管理。 |
| **边界内** | Room 状态机、Member 信息模型、Theme 选择逻辑、Commute Fairness 算法、Gathering Point 计算、Per-member Route 生成、Itinerary Version 管理、AI Revision 编排。 |
| **边界外** | 活动搜索实现（DD-05/DD-06/DD-17）；路线计算（DD-04 AMap）；证据定级（DD-03）；前端渲染（DD-19）；跨城编排（DD-02）；对话式交互（DD-15）。 |
| **架构位置** | DD-02 同级的**独立 LangGraph 子图**：`RoomPlanGraph`（thread_id=`room:{id}`），与 `TripPlanGraph` 并存。 |

---

## 2. 数据模型（新增表，DD-01 扩展）

### 2.1 Room（活动房间）

```sql
CREATE TABLE rooms (
    id              BIGSERIAL PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'DRAFT',
    -- DRAFT|COLLECTING|THEME_SELECTING|RECOMMENDING|ACTIVITY_SELECTED|PLANNING|PUBLISHED|EXPIRED
    activity_date   DATE NOT NULL,
    city            TEXT NOT NULL DEFAULT '上海',
    time_window     JSONB,         -- {"earliest": "14:00", "latest": "21:00"}
    budget_range    JSONB,         -- {"min": 0, "max": 200, "currency": "CNY"}
    theme           TEXT,           -- 确定后的主题
    theme_method    TEXT,           -- direct|vote|ai|wheel
    creator_id      TEXT NOT NULL,
    plan_id         BIGINT REFERENCES plans(id),  -- 完成后关联 Plan（统一记忆/历史）
    thread_id       TEXT NOT NULL,  -- LangGraph thread_id = "room:{id}"
    invite_code     TEXT NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expire_at       TIMESTAMPTZ NOT NULL,  -- 活动结束后 7 天
    CONSTRAINT valid_status CHECK (status IN (
        'DRAFT','COLLECTING','THEME_SELECTING','RECOMMENDING',
        'ACTIVITY_SELECTED','PLANNING','PUBLISHED','EXPIRED'))
);
```

### 2.2 RoomMember（房间成员）

```sql
CREATE TABLE room_members (
    id              BIGSERIAL PRIMARY KEY,
    room_id         BIGINT NOT NULL REFERENCES rooms(id),
    nickname        TEXT NOT NULL,
    member_token    TEXT NOT NULL UNIQUE,  -- 轻量认证
    origin_name     TEXT,          -- 出发地名称（地铁站/商圈）
    origin_geo      GEOGRAPHY(POINT, 4326),  -- PostGIS 坐标（与 party_constraints.origin_geo 一致；实现将原 GEOMETRY 对齐为 Geography）
    origin_poi_id   TEXT,          -- 高德 POI ID
    earliest_depart TEXT,          -- "14:00"
    latest_end      TEXT,          -- "21:00"
    budget          INT,           -- 人均预算（分）
    interests       TEXT[],        -- 兴趣标签
    hard_constraints TEXT[],       -- 硬性约束
    negative_prefs  TEXT[],        -- 不接受的
    transport_pref  TEXT,          -- walk|transit|drive|any
    note            TEXT,          -- 自由备注
    submitted_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 2.3 ThemeVote（主题投票）

```sql
CREATE TABLE theme_votes (
    id          BIGSERIAL PRIMARY KEY,
    room_id     BIGINT NOT NULL REFERENCES rooms(id),
    member_id   BIGINT NOT NULL REFERENCES room_members(id),
    theme       TEXT NOT NULL,
    weight      INT NOT NULL DEFAULT 1,  -- 1=可接受, 3=强烈喜欢, -2=不喜欢
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(room_id, member_id, theme)
);
```

### 2.4 RoomItinerary（房间行程版本）

```sql
CREATE TABLE room_itineraries (
    id          BIGSERIAL PRIMARY KEY,
    room_id     BIGINT NOT NULL REFERENCES rooms(id),
    version     INT NOT NULL DEFAULT 1,
    payload     JSONB NOT NULL,  -- 完整行程 JSON
    is_current  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## 3. 房间状态机（LangGraph 子图）

### 3.1 状态定义

```python
class RoomState(TypedDict):
    room_id: int
    status: str                      # 当前房间状态
    members: list[dict]              # 所有成员信息
    common_time_window: dict | None  # 计算后的共同时间窗
    theme: str | None                # 确定的主题
    theme_candidates: list[dict]     # 候选主题（带权重）
    activity_candidates: list[dict]  # 候选活动（含通勤矩阵）
    selected_activity: dict | None   # 选定的活动
    gathering: dict | None           # 集合点/时间
    member_routes: list[dict]        # 每人路线
    itinerary: dict | None           # 当前行程
    itinerary_version: int           # 版本号
    warnings: list[str]
    errors: list[str]
```

### 3.2 状态转换图

```text
DRAFT
  │ (创建者填写基本信息)
  ▼
COLLECTING
  │ (所有成员提交信息 / 超时)
  ▼
THEME_SELECTING
  │ (直选/投票/AI/转盘 → 确定主题)
  ▼
RECOMMENDING
  │ (DD-17 深研 + DD-05 检索 → 候选活动 → 通勤矩阵)
  ▼
ACTIVITY_SELECTED
  │ (用户选定 / 投票选定)
  ▼
PLANNING
  │ (集合点/时间 + 多人路线 + 行程生成)
  ▼
PUBLISHED
  │ (生成分享卡片)
  │
  └─── EXPIRED (活动结束后 7 天)
```

### 3.3 节点定义

| 节点 | 读入 | 写出 | 调用 |
|------|------|------|------|
| `collect_members` | members | common_time_window, warnings | 时间窗计算 |
| `select_theme` | members, theme_candidates | theme | 投票/转盘/AI 逻辑 |
| `research_activities` | theme, common_time_window, members | activity_candidates | DD-17 深研 + DD-05 检索 |
| `rank_activities` | activity_candidates, members | activity_candidates(排序后) | 通勤公平性 + 匹配分 |
| `confirm_activity` | 用户选择 | selected_activity | - |
| `plan_gathering` | selected_activity, members | gathering, member_routes | DD-04 AMap |
| `generate_itinerary` | 全部 | itinerary | 时间线编排 |
| `publish` | itinerary | - | 写 room_itineraries + 生成分享 |

---

## 4. 核心算法

### 4.1 共同时间窗计算

```python
def compute_common_window(members: list[dict]) -> dict:
    """计算所有成员的共同空闲时间窗。"""
    from datetime import time
    latest_start = max(
        time.fromisoformat(m["earliest_depart"]) for m in members if m.get("earliest_depart")
    )
    earliest_end = min(
        time.fromisoformat(m["latest_end"]) for m in members if m.get("latest_end")
    )
    available_hours = (
        earliest_end.hour * 60 + earliest_end.minute -
        latest_start.hour * 60 - latest_start.minute
    ) / 60
    feasible = available_hours >= 2.0  # 至少 2 小时
    return {
        "start": latest_start.isoformat(),
        "end": earliest_end.isoformat(),
        "available_hours": round(available_hours, 1),
        "feasible": feasible,
        "suggestions": [] if feasible else _suggest_adjustments(members, available_hours),
    }
```

### 4.2 主题转盘加权随机

```python
import random

def weighted_wheel(
    themes: list[str],
    members: list[dict],
    weather: dict | None = None,
    hard_excluded: set[str] | None = None,
) -> tuple[str, list[dict]]:
    """GoMate PRD §7.3.3：受约束的加权随机。
    返回 (选中主题, 各主题权重明细)。"""
    hard_excluded = hard_excluded or set()
    weights = []
    for theme in themes:
        if theme in hard_excluded:
            continue
        w = 0
        for m in members:
            if theme in (m.get("interests") or []):
                w += 3  # 强烈喜欢
            elif theme not in (m.get("negative_prefs") or []):
                w += 1  # 可接受
            else:
                w -= 2  # 不喜欢
        # 天气适配
        if weather and _theme_fits_weather(theme, weather):
            w += 1
        # 时间长度适配
        w += 1  # 简化：市内活动通常都适配半日
        if w > 0:
            weights.append({"theme": theme, "weight": w})
    if not weights:
        # 全部被过滤 → 降级返回权重最高的被排除项
        return themes[0], []
    selected = random.choices(
        [w["theme"] for w in weights],
        weights=[w["weight"] for w in weights],
        k=1
    )[0]
    return selected, weights
```

### 4.3 通勤公平性计算

```python
import math

def commute_fairness_score(commute_times: list[int]) -> float:
    """GoMate PRD §7.4.6：通勤公平性得分。
    commute_times: 每位成员到活动地点的通勤时间（分钟）。
    得分越低越公平。使用方差 + 最大值惩罚。"""
    n = len(commute_times)
    if n == 0:
        return 0.0
    mean = sum(commute_times) / n
    variance = sum((t - mean) ** 2 for t in commute_times) / n
    max_penalty = max(commute_times) * 0.3  # 对最远成员额外惩罚
    return math.sqrt(variance) + max_penalty


def rank_by_fairness(
    activities: list[dict],
    members: list[dict],
    commute_matrix: dict[str, list[int]],
) -> list[dict]:
    """对候选活动按综合得分排序。
    GoMate PRD §7.4.5 权重：兴趣30% + 时间20% + 通勤公平20% + 可信度10% + 预算10% + 天气5% + 新鲜5%
    """
    scored = []
    for act in activities:
        act_id = act["id"]
        times = commute_matrix.get(act_id, [])
        fairness = commute_fairness_score(times) if times else 999
        # 综合打分（简化示例，实际各维度分别计算后加权）
        interest_score = _interest_match(act, members) * 0.30
        time_score = _time_match(act, members) * 0.20
        fairness_score = (1 - min(fairness / 120, 1.0)) * 0.20  # 归一化
        trust_score = _trust_score(act) * 0.10
        budget_score = _budget_match(act, members) * 0.10
        weather_score = _weather_match(act) * 0.05
        novelty_score = 0.05  # 默认新鲜
        total = interest_score + time_score + fairness_score + trust_score + budget_score + weather_score + novelty_score
        scored.append({**act, "match_score": round(total, 4), "commute_fairness": round(fairness, 1)})
    scored.sort(key=lambda x: -x["match_score"])
    return scored
```

### 4.4 集合点与集合时间计算

```python
from datetime import datetime, timedelta

def compute_gathering(
    activity: dict,
    members: list[dict],
    member_routes: list[dict],
) -> dict:
    """GoMate PRD §7.6.3/§7.6.4：计算集合时间和集合点。"""
    # 活动开始时间
    activity_start = datetime.fromisoformat(activity["start_at"])
    # 建议提前 15 分钟到达
    target_arrival = activity_start - timedelta(minutes=15)

    # 集合点优先级：场馆入口 > 附近地铁出口 > 明显地标 > 第一段用餐地
    gathering_point = _pick_gathering_point(activity)

    # 倒推每人出发时间
    departures = []
    for route in member_routes:
        buffer = _get_buffer(route["transport_mode"])  # 公交10min/驾车15min/步行5min
        depart_time = target_arrival - timedelta(minutes=route["duration_min"] + buffer)
        departures.append({
            "member_id": route["member_id"],
            "nickname": route["nickname"],
            "suggested_departure": depart_time.isoformat(),
            "estimated_arrival": (target_arrival - timedelta(minutes=buffer)).isoformat(),
            "duration_min": route["duration_min"],
            "transport_mode": route["transport_mode"],
        })

    return {
        "gathering_point": gathering_point,
        "target_time": target_arrival.isoformat(),
        "member_departures": departures,
    }


def _get_buffer(mode: str) -> int:
    return {"transit": 10, "driving": 15, "walking": 5, "bicycling": 5}.get(mode, 10)


def _pick_gathering_point(activity: dict) -> dict:
    """优先级：场馆入口 > 附近地铁站出口 > 地标。"""
    if activity.get("entrance_poi"):
        return {"name": activity["entrance_poi"]["name"], "type": "entrance",
                "coords": activity["entrance_poi"]["coords"]}
    if activity.get("nearby_metro"):
        return {"name": activity["nearby_metro"]["name"] + "出口", "type": "metro",
                "coords": activity["nearby_metro"]["coords"]}
    return {"name": activity.get("venue", "活动地点"), "type": "venue",
            "coords": activity.get("coords")}
```

### 4.5 活动推荐排序综合得分

```python
def _interest_match(act: dict, members: list[dict]) -> float:
    """活动主题与成员兴趣匹配度 [0,1]。"""
    cat = act.get("category", "")
    tags = set(act.get("tags", []))
    matched = 0
    for m in members:
        member_interests = set(m.get("interests", []))
        if cat in member_interests or tags & member_interests:
            matched += 1
    return matched / max(len(members), 1)


def _time_match(act: dict, members: list[dict]) -> float:
    """活动时间是否在共同时间窗内 [0,1]。"""
    # 简化：活动在共同窗口内 = 1.0，部分重叠 = 0.5，不重叠 = 0
    return 1.0  # 由 research 阶段已过滤


def _trust_score(act: dict) -> float:
    """活动信息可信度 [0,1]。"""
    status = (act.get("evidence") or {}).get("verification_status", "unknown")
    return {"official_source_confirmed": 1.0, "public_source_observed": 0.7,
            "estimated": 0.4, "unknown": 0.2, "expired": 0.1}.get(status, 0.3)


def _budget_match(act: dict, members: list[dict]) -> float:
    """预算匹配度 [0,1]。"""
    price = act.get("price_cents", 0)
    if price == 0:
        return 1.0  # 免费
    budgets = [m.get("budget", 20000) for m in members]  # 默认 200 元
    min_budget = min(budgets)
    if price <= min_budget:
        return 1.0
    elif price <= min_budget * 1.5:
        return 0.5
    return 0.0


def _weather_match(act: dict) -> float:
    """天气适配 [0,1]。简化：室内活动总是 1.0。"""
    return 1.0 if act.get("indoor") else 0.7
```

---

## 5. AI 自然语言修改（局部更新）

### 5.1 修改类型识别

```python
from enum import Enum

class RevisionType(Enum):
    REPLACE_NODE = "replace_node"       # 替换某个节点（换活动/换餐厅）
    ADD_NODE = "add_node"               # 新增节点
    REMOVE_NODE = "remove_node"         # 删除节点
    ADJUST_TIME = "adjust_time"         # 调整时间
    ADJUST_BUDGET = "adjust_budget"     # 调整预算
    CHANGE_TRANSPORT = "change_transport"  # 修改交通方式
    CHANGE_THEME = "change_theme"       # 修改整体主题
    FULL_REPLAN = "full_replan"         # 重新规划全部
```

### 5.2 局部修改原则

GoMate PRD §7.8.3：
- 默认只修改受影响部分
- 保留核心活动（除非明确要求换）
- 保留成员出发路线（除非出发时间变化）
- 重新计算受影响的后续节点时间
- 不重新生成无关节点

### 5.3 修改确认规则

涉及以下变化时需确认（GoMate PRD §7.8.4）：
- 删除核心活动
- 整体延后导致成员无法按时结束
- 预算明显超出成员限制
- 活动需要重新购票或预约
- 修改后某位成员通勤明显增加

---

## 6. 行程版本管理

```python
def save_itinerary_version(room_id: int, payload: dict) -> int:
    """保存新版本，将旧版本标记为非当前。至少保留当前+上一个版本。"""
    with get_session() as s:
        # 取消当前版本标记
        s.execute(
            "UPDATE room_itineraries SET is_current = FALSE WHERE room_id = :rid AND is_current = TRUE",
            {"rid": room_id}
        )
        # 获取最新版本号
        max_ver = s.scalar(
            "SELECT COALESCE(MAX(version), 0) FROM room_itineraries WHERE room_id = :rid",
            {"rid": room_id}
        )
        new_ver = max_ver + 1
        itinerary = RoomItinerary(room_id=room_id, version=new_ver, payload=payload, is_current=True)
        s.add(itinerary)
        s.flush()
        # 清理超过 5 个历史版本
        _cleanup_old_versions(s, room_id, keep=5)
        return new_ver


def undo_itinerary(room_id: int) -> dict | None:
    """撤销：回退到上一个版本。GoMate PRD §7.8.5 支持"撤销本次修改"。"""
    with get_session() as s:
        prev = s.execute(
            """SELECT * FROM room_itineraries
               WHERE room_id = :rid AND is_current = FALSE
               ORDER BY version DESC LIMIT 1""",
            {"rid": room_id}
        ).first()
        if not prev:
            return None
        # 将当前版本取消，恢复上一版本
        s.execute("UPDATE room_itineraries SET is_current = FALSE WHERE room_id = :rid", {"rid": room_id})
        s.execute("UPDATE room_itineraries SET is_current = TRUE WHERE id = :id", {"id": prev.id})
        return prev.payload
```

---

## 7. 与其他模块接线

| 模块 | 接线方式 | 说明 |
|------|---------|------|
| **DD-02 编排** | 并存 | RoomPlanGraph 是独立子图，通过 `thread_id=room:{id}` 区分 |
| **DD-04 AMap** | 调用 | `route_planning`（多人路线）+ `geocode`（出发地解析）+ `poi_search`（集合点） |
| **DD-05 检索** | 调用 | `retrieve_activities(scope="local", city="上海", theme=..., date=...)` |
| **DD-06 情报** | 被消费 | 市内活动同走情报流水线，`ingest_realtime` 支持市内 |
| **DD-07 约束** | 复用+扩展 | 复用 `missing_slots` 逻辑；新增房间制聚合 + 冲突检测 |
| **DD-11 餐饮** | 调用 | 行程中餐饮推荐复用 DD-11 逻辑（动线契合 + 忌讳排除） |
| **DD-15 Copilot** | 被驱动 | 对话入口可创建房间 / 加入房间 / 修改行程 |
| **DD-16 记忆** | 读写 | 房间完成后抽取偏好写记忆；新会话读记忆减少提问 |
| **DD-17 深研** | 调用 | `deep_research(query="上海 本周末 {theme}", scope="local")` |
| **DD-19 前端** | SSE 消费 | 前端按 room_state/theme_result/activity_candidates 等事件渲染 |

---

## 8. BFF API 契约

| 方法 | 路径 | 说明 | 返回 |
|------|------|------|------|
| POST | `/rooms` | 创建活动房间 | `{room_id, invite_code, invite_url}` |
| GET | `/rooms/{id}` | 获取房间状态与成员 | Room + Members |
| POST | `/rooms/{id}/members` | 成员加入（昵称+token） | `{member_id, member_token}` |
| PUT | `/rooms/{id}/members/{mid}` | 更新成员信息 | 200 |
| GET | `/rooms/{id}/summary` | 聚合摘要（共同时间/冲突/偏好） | Summary JSON |
| POST | `/rooms/{id}/theme/vote` | 提交主题投票 | 200 |
| POST | `/rooms/{id}/theme/wheel` | 触发转盘 | `{theme, weights}` |
| POST | `/rooms/{id}/theme/confirm` | 确认主题 | 200 |
| GET | `/rooms/{id}/recommend` | SSE 活动推荐流（含深研进度） | EventSource |
| POST | `/rooms/{id}/select-activity` | 选定活动 | 200 |
| GET | `/rooms/{id}/routes` | 获取各成员路线 | `{gathering, member_routes}` |
| GET | `/rooms/{id}/plan` | 获取当前行程 | Itinerary JSON |
| POST | `/rooms/{id}/plan/modify` | AI 修改行程 | SSE 修改流 |
| POST | `/rooms/{id}/plan/undo` | 撤销上次修改 | Itinerary JSON |
| GET | `/rooms/{id}/share` | 获取分享数据（脱敏） | Share JSON |

---

## 9. 降级设计

| 场景 | 降级策略 |
|------|---------|
| DD-17 深研超时 | 返回 DD-05 库内已有活动（标注"建议到官方确认"），深研结果后续异步补充 |
| AMap 路线失败 | 展示直线距离 + 地图跳转链接 + 提示用户在地图 App 确认 |
| 共同时间窗不足 2 小时 | 提示缩短活动时长 / 允许部分成员中途加入 |
| 所有活动被硬约束过滤 | 建议放宽约束 + 展示被过滤原因 |
| AI 修改意图识别失败 | 回退为"换一批活动"兜底 |
| 转盘所有权重<=0 | 降级为完全随机（从硬约束满足的主题中选） |

---

## 10. 效果与验收标准

| 项 | 验收标准 |
|---|---|
| 房间状态流转 | 8 个状态顺序流转正确，异常不阻塞 |
| 多人时间窗 | 3 人不同时间 → 正确计算交集 + 不足时给建议 |
| 主题转盘 | 硬约束被排除 + 偏好加权 + 一次反悔可用 |
| 通勤公平性 | 5 个候选活动排序后最公平的排前面 |
| 集合时间 | 倒推出发时间 + 缓冲合理 |
| AI 修改 | "换一家不辣的餐厅" → 只替换餐饮节点，其他不动 |
| 版本管理 | 修改后可撤销回到上一版 |
| 深研覆盖 | 市内活动同走 DD-17 + 证据六态 |
| 分享脱敏 | 分享卡不含精确出发地/经纬度/联系方式 |

---

## 11. 风险

| 风险 | 缓解 |
|------|------|
| 成员不填写信息导致房间卡住 | 超时（2小时）自动进入下一阶段，用缺省值 |
| 转盘结果不满意 | 支持一次反悔；也可直接选择其他主题 |
| 多人通勤差距过大 | 推荐更公平的备选 + 显示差距原因 |
| 市内活动数据覆盖不足 | DD-17 深研兜底 + MVP 人工种子数据 |
| 行程修改频繁导致版本膨胀 | 只保留最近 5 个版本 |

---

> 本文与既有 DD 对齐声明：Room 子图与 DD-02 TripPlan 图并存不冲突；活动检索复用 DD-05 + DD-17（增加 `scope=local`）；路线计算复用 DD-04 AMap；证据体系完全复用 DD-03；记忆复用 DD-16；前端渲染见 DD-19。
