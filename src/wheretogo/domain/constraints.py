"""DD-07 约束收集与多人聚合（parse 节点的领域实现）。

约束是"输入"非"对外事实"——parse 不受 Guard。产出 DD-01 §8.1 `plans.constraints` schema。
"""
from __future__ import annotations

from ..schemas.constraints import build_rerank_query
from .timeutil import upcoming_weekend


def apply_defaults(c: dict | None) -> dict:
    c = dict(c or {})
    c.setdefault("party_size", 1)
    c.setdefault("origins", [])
    c.setdefault("interests", [])  # 空=全品类调研，不强制用户填
    c.setdefault("soft_preferences", [])
    c.setdefault("experience_requirements", [])
    c.setdefault("research_goal", "")
    c.setdefault("acceptance_criteria", [])
    c.setdefault("dietary", [])
    c.setdefault("hard_constraints", [])
    c.setdefault("budget_band", {})
    c.setdefault("accept_flight", True)
    c.setdefault("accept_night_train", False)
    # 无目的地 → target_city_code 留空（不静默默认城市）：
    # discover 节点用解析出的出发地做同城推荐，兜底在 parse_constraints 的 warnings 显式声明
    # 无 weekend → 默认本周末（周五18:00~周日23:59，覆盖周五晚场活动）
    if not c.get("weekend_start"):
        from datetime import timedelta
        sat, sun = upcoming_weekend()
        fri_evening = sat - timedelta(hours=6)  # 周五18:00
        sun_night = sun.replace(hour=23, minute=59, second=59)
        c["weekend_start"] = fri_evening.isoformat()
        c["weekend_end"] = sun_night.isoformat()
    return c


def missing_slots(c: dict) -> list[str]:
    """关键缺失槽位——只有出发地是真正必须问的。

    时间默认本周末，兴趣默认全品类调研，目的地默认当地——AI 主动推荐而非等用户全部想好。
    """
    miss: list[str] = []
    if not c.get("origins"):
        miss.append("origins")
    return miss


def parse_constraints(raw: dict | None) -> tuple[dict, list[str]]:
    """结构化 + 缺省 + 检索 query；返回 (constraints, warnings)。不产事实。"""
    c = apply_defaults(raw)
    warnings: list[str] = []
    if not c.get("origins") or c["origins"] == []:
        warnings.append("未提供出发地，按同城处理")
    if not c.get("target_city_code"):
        # 显式声明兜底（不静默）：目的地留空 → discover 用解析出的出发地做同城推荐
        warnings.append("未指定目的地，按出发地所在城市做同城推荐")
    if not c.get("query"):
        # 无兴趣/忌讳等个性化信号时 build_rerank_query 返回空串：query 留空 →
        # 检索层据此跳过 rerank，仅保留结构化过滤 + 时间窗（不再拼"周末不限 忌讳无"废话串）
        c["query"] = build_rerank_query(c)
    return c, warnings


def intersect_bands(b1: dict | None, b2: dict | None) -> dict:
    """预算区间交集；空交集→取并集包络 + _conflict 标记（DD-07）。"""
    b1, b2 = b1 or {}, b2 or {}
    mins = [x for x in (b1.get("min"), b2.get("min")) if x is not None]
    maxs = [x for x in (b1.get("max"), b2.get("max")) if x is not None]
    lo = max(mins) if mins else None
    hi = min(maxs) if maxs else None
    if lo is not None and hi is not None and lo > hi:  # 空交集
        return {"min": (min(mins) if mins else None), "max": (max(maxs) if maxs else None), "_conflict": True}
    return {"min": lo, "max": hi}


def aggregate_party(members: list[dict]) -> dict:
    """多人公平聚合：earliest=max、latest=min、预算交集、flight/night_train=all、兴趣/忌讳=并集。"""
    if not members:
        return {}
    earliest = max((m["earliest_depart"] for m in members if m.get("earliest_depart")), default=None)
    latest = min((m["latest_return"] for m in members if m.get("latest_return")), default=None)
    band: dict = {}
    for m in members:
        band = intersect_bands(band, m.get("budget_band"))
    interests = sorted(set().union(*[set(m.get("interests") or []) for m in members]))
    soft_preferences = sorted(
        set().union(*[set(m.get("soft_preferences") or []) for m in members])
    )
    dietary = sorted(set().union(*[set(m.get("dietary") or []) for m in members]))
    return {
        "earliest_depart": earliest, "latest_return": latest, "budget_band": band,
        "accept_flight": all(m.get("accept_flight", True) for m in members),
        "accept_night_train": all(m.get("accept_night_train", False) for m in members),
        "interests": interests, "soft_preferences": soft_preferences,
        "dietary": dietary, "party_size": len(members),
    }
