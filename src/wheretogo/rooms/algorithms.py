"""DD-18 §4 核心算法：共同时间窗 / 主题转盘 / 通勤公平性 / 集合点与集合时间 / 综合排序。

纯函数、无 IO——上层（RoomPlanGraph 节点 / RoomService）注入数据。
"""
from __future__ import annotations

import math
import random
from datetime import datetime, time, timedelta

#: 无任何成员兴趣时的兜底主题池（市内周末常见品类）
DEFAULT_THEMES = ["展览", "演出", "市集", "户外", "美食", "桌游", "运动", "手作"]

#: 室内主题（天气恶劣时加权；DD-18 §4.2 天气适配）
_INDOOR_THEMES = {"展览", "演出", "桌游", "手作", "美食", "电影", "密室", "livehouse"}

#: 出发缓冲（分钟）：公交10/驾车15/步行5/骑行5（DD-18 §4.4）
_MODE_BUFFER = {"transit": 10, "driving": 15, "walking": 5, "bicycling": 5}


# ============================ §4.1 共同时间窗 ============================
def compute_common_window(members: list[dict]) -> dict:
    """计算所有成员的共同空闲时间窗；不足 2 小时给调整建议。"""
    starts = [m["earliest_depart"] for m in members if m.get("earliest_depart")]
    ends = [m["latest_end"] for m in members if m.get("latest_end")]
    if not starts or not ends:
        return {"start": None, "end": None, "available_hours": None,
                "feasible": True, "suggestions": []}
    latest_start = max(time.fromisoformat(s) for s in starts)
    earliest_end = min(time.fromisoformat(e) for e in ends)
    available_hours = (
        earliest_end.hour * 60 + earliest_end.minute
        - latest_start.hour * 60 - latest_start.minute
    ) / 60
    feasible = available_hours >= 2.0  # 至少 2 小时
    return {
        "start": latest_start.isoformat(timespec="minutes"),
        "end": earliest_end.isoformat(timespec="minutes"),
        "available_hours": round(available_hours, 1),
        "feasible": feasible,
        "suggestions": [] if feasible else _suggest_adjustments(members, available_hours),
    }


def _suggest_adjustments(members: list[dict], available_hours: float) -> list[str]:
    """共同窗不足 2h：指出最紧的成员 + 给可执行建议（DD-18 §9 降级）。"""
    suggestions: list[str] = []
    with_start = [m for m in members if m.get("earliest_depart")]
    with_end = [m for m in members if m.get("latest_end")]
    if with_start:
        tightest = max(with_start, key=lambda m: time.fromisoformat(m["earliest_depart"]))
        suggestions.append(
            f"{tightest.get('nickname', '某位成员')} 最早 {tightest['earliest_depart']} 才能出发，"
            "可考虑其提前出发或中途加入"
        )
    if with_end:
        earliest = min(with_end, key=lambda m: time.fromisoformat(m["latest_end"]))
        suggestions.append(
            f"{earliest.get('nickname', '某位成员')} 需要 {earliest['latest_end']} 前结束，"
            "可考虑缩短活动时长或其提前离场"
        )
    if available_hours is not None and available_hours < 2.0:
        suggestions.append("共同时间不足 2 小时，建议选择半小时至一小时的轻量活动")
    return suggestions


# ============================ §4.2 主题转盘加权随机 ============================
def _theme_fits_weather(theme: str, weather: dict) -> bool:
    """天气恶劣（雨/高温预警）→ 室内主题适配。"""
    if weather.get("adverse") or weather.get("indoor_pref"):
        return theme in _INDOOR_THEMES
    return True


def weighted_wheel(
    themes: list[str],
    members: list[dict],
    weather: dict | None = None,
    hard_excluded: set[str] | None = None,
    rng: random.Random | None = None,
) -> tuple[str, list[dict]]:
    """GoMate PRD §7.3.3：受约束的加权随机。返回 (选中主题, 各主题权重明细)。

    权重：成员兴趣 +3 / 可接受 +1 / 不喜欢 -2；天气适配 +1；时长适配 +1。
    全部被过滤 → 降级为完全随机（从硬约束满足的主题中选，DD-18 §9）。
    """
    rng = rng or random
    hard_excluded = hard_excluded or set()
    allowed = [t for t in themes if t not in hard_excluded]
    if not allowed:  # 全部被硬约束排除 → 只能回原池随机
        allowed = list(themes)
    weights: list[dict] = []
    for theme in allowed:
        w = 0
        for m in members:
            if theme in (m.get("interests") or []):
                w += 3  # 强烈喜欢
            elif theme not in (m.get("negative_prefs") or []):
                w += 1  # 可接受
            else:
                w -= 2  # 不喜欢
        if weather and _theme_fits_weather(theme, weather):
            w += 1  # 天气适配
        w += 1  # 时长适配（市内活动通常都适配半日）
        if w > 0:
            weights.append({"theme": theme, "weight": w})
    if not weights:
        # 所有权重<=0 → 降级完全随机（硬约束满足的主题中选）
        return rng.choice(allowed), []
    selected = rng.choices(
        [w["theme"] for w in weights], weights=[w["weight"] for w in weights], k=1
    )[0]
    return selected, weights


def tally_votes(votes: list[dict]) -> list[dict]:
    """投票计票：同主题权重求和，按总分降序。votes: [{theme, weight}]。"""
    totals: dict[str, int] = {}
    for v in votes:
        totals[v["theme"]] = totals.get(v["theme"], 0) + int(v.get("weight", 1))
    return [{"theme": t, "score": s}
            for t, s in sorted(totals.items(), key=lambda kv: -kv[1])]


def candidate_themes(members: list[dict]) -> list[str]:
    """候选主题池：成员兴趣并集 + 兜底池去重（保持顺序）。"""
    pool: list[str] = []
    for m in members:
        for t in m.get("interests") or []:
            if t not in pool:
                pool.append(t)
    for t in DEFAULT_THEMES:
        if t not in pool:
            pool.append(t)
    return pool


def hard_excluded_themes(members: list[dict]) -> set[str]:
    """硬性约束排除的主题：成员 hard_constraints 并集。"""
    excluded: set[str] = set()
    for m in members:
        excluded |= set(m.get("hard_constraints") or [])
    return excluded


# ============================ §4.3 通勤公平性 ============================
def commute_fairness_score(commute_times: list[int]) -> float:
    """GoMate PRD §7.4.6：得分越低越公平。sqrt(方差) + 最大值*0.3 惩罚。"""
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
    weather: dict | None = None,
) -> list[dict]:
    """候选活动综合排序（GoMate PRD §7.4.5 权重）：
    兴趣30% + 时间20% + 通勤公平20% + 可信度10% + 预算10% + 天气5% + 新鲜5%。
    """
    scored = []
    for act in activities:
        act_id = str(act.get("id"))
        times = commute_matrix.get(act_id, [])
        fairness = commute_fairness_score(times) if times else 999.0
        interest_score = _interest_match(act, members) * 0.30
        time_score = _time_match(act, members) * 0.20
        fairness_score = (1 - min(fairness / 120, 1.0)) * 0.20  # 归一化到 [0,1]
        trust_score = _trust_score(act) * 0.10
        budget_score = _budget_match(act, members) * 0.10
        weather_score = _weather_match(act, weather) * 0.05
        novelty_score = 0.05  # 默认新鲜（深研当周入库即新）
        total = (interest_score + time_score + fairness_score + trust_score
                 + budget_score + weather_score + novelty_score)
        scored.append({
            **act,
            "match_score": round(total, 4),
            "commute_fairness": round(fairness, 1),
            "commute_times": times,
        })
    scored.sort(key=lambda x: -x["match_score"])
    return scored


# ============================ §4.5 排序分项 ============================
def _interest_match(act: dict, members: list[dict]) -> float:
    """活动主题与成员兴趣匹配度 [0,1]。"""
    cat = act.get("category", "") or ""
    tags = set(act.get("tags", []) or [])
    matched = 0
    for m in members:
        member_interests = set(m.get("interests") or [])
        if (cat and cat in member_interests) or (tags & member_interests):
            matched += 1
    return matched / max(len(members), 1)


def _time_match(act: dict, members: list[dict]) -> float:
    """活动时间是否在共同时间窗内 [0,1]；research 阶段已按窗口过滤 → 1.0。"""
    return 1.0


def _trust_score(act: dict) -> float:
    """活动信息可信度 [0,1]：直接映射证据六态（DD-03）。"""
    status = act.get("verification_status") or (
        (act.get("evidence") or {}).get("verification_status", "unknown")
    )
    return {
        "confirmed_by_user": 1.0, "official_source_confirmed": 1.0,
        "public_source_observed": 0.7, "estimated": 0.4,
        "unknown": 0.2, "expired": 0.1,
    }.get(status, 0.3)


def _budget_match(act: dict, members: list[dict]) -> float:
    """预算匹配度 [0,1]：价格 <= 最小成员预算=1.0；<=1.5倍=0.5；超出=0。"""
    price = act.get("price_cents", 0) or 0
    if price == 0:
        return 1.0  # 免费/未知价按免费乐观处理
    budgets = [m.get("budget") for m in members if m.get("budget")]
    min_budget = min(budgets) if budgets else 20000  # 默认 200 元
    if price <= min_budget:
        return 1.0
    if price <= min_budget * 1.5:
        return 0.5
    return 0.0


def _weather_match(act: dict, weather: dict | None = None) -> float:
    """天气适配 [0,1]：恶劣天气下室内=1.0、户外=0.3；正常天气一律 1.0。"""
    if weather and (weather.get("adverse") or weather.get("indoor_pref")):
        return 1.0 if act.get("indoor") else 0.3
    return 1.0 if act.get("indoor") else 0.7


# ============================ §4.4 集合点与集合时间 ============================
def compute_gathering(
    activity: dict,
    members: list[dict],
    member_routes: list[dict],
) -> dict:
    """GoMate PRD §7.6.3/§7.6.4：集合时间（提前 15 分钟）+ 倒推每人出发时间。"""
    start_iso = activity.get("start_at")
    try:
        activity_start = datetime.fromisoformat(start_iso) if start_iso else None
    except (ValueError, TypeError):
        activity_start = None
    if activity_start is None:  # 无开始时间 → 无法倒推（estimated 标注由上层加）
        return {"gathering_point": _pick_gathering_point(activity),
                "target_time": None, "member_departures": []}
    target_arrival = activity_start - timedelta(minutes=15)  # 建议提前 15 分钟到达
    gathering_point = _pick_gathering_point(activity)

    departures = []
    for route in member_routes:
        buffer = _get_buffer(route.get("transport_mode", "transit"))
        duration = int(route.get("duration_min") or 0)
        depart_time = target_arrival - timedelta(minutes=duration + buffer)
        departures.append({
            "member_id": route.get("member_id"),
            "nickname": route.get("nickname"),
            "suggested_departure": depart_time.isoformat(),
            "estimated_arrival": (target_arrival - timedelta(minutes=buffer)).isoformat(),
            "duration_min": duration,
            "transport_mode": route.get("transport_mode", "transit"),
        })
    return {
        "gathering_point": gathering_point,
        "target_time": target_arrival.isoformat(),
        "member_departures": departures,
    }


def _get_buffer(mode: str) -> int:
    return _MODE_BUFFER.get(mode, 10)


def _pick_gathering_point(activity: dict) -> dict:
    """集合点优先级：场馆入口 > 附近地铁出口 > 场馆本身（DD-18 §4.4）。"""
    if activity.get("entrance_poi"):
        return {"name": activity["entrance_poi"]["name"], "type": "entrance",
                "coords": activity["entrance_poi"].get("coords")}
    if activity.get("nearby_metro"):
        return {"name": activity["nearby_metro"]["name"] + "出口", "type": "metro",
                "coords": activity["nearby_metro"].get("coords")}
    return {"name": activity.get("venue") or "活动地点", "type": "venue",
            "coords": activity.get("location") or activity.get("coords")}
