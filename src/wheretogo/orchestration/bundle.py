"""Trip Bundle 组装（DD-13 职责的最小实现；探索版/确认版）。

bundle 为“渲染就绪”结构，每个事实字段随身带 evidence（前端据此渲染六态；DD-03/增补 B）。
"""
from __future__ import annotations

import re

from .state import TripPlanState


# DD-13 §3.3 横切声明（两版各一）
_DISCLAIMER_EXPLORE = "票价/余票/起售时间以官方平台当前页面为准；带“估算/待确认”标记的字段非最终值"
_DISCLAIMER_CONFIRM = "票价/余票以官方平台为准；预估待花为估算值（estimated）"


def compose_explore_bundle(state: TripPlanState) -> dict:
    """探索版：城市卡 + 活动 + 交通策略（可分享，等回填）；含§06 主题/时间窗/预算/住宿/待确认。"""
    from ..domain.compose import build_presale_reminders, reminders_preview  # 惰性导入防循环

    c = state.get("constraints") or {}
    payload = {
        "version": "explore",
        "plan_id": state.get("plan_id"),
        "theme": _theme(c, state.get("activities")),
        "time_windows": _time_windows(c),
        "budget_range": _budget_range(c),
        "lodging_area": _lodging_hint(state),
        "cities": _overlay_primary_count(state.get("candidate_cities", []), state.get("activities")),
        "activities": state.get("activities", []),
        "research_outcome": state.get("research_outcome"),
        "transport": state.get("transport_options", {}),
        "pending_checklist": _pending_checklist(state),
        "warnings": _current_warnings(state),
        "assistant_response": state.get("assistant_response"),
        "itinerary_draft": state.get("itinerary_draft", []),
        "plan_ledger": state.get("plan_ledger", {}),
        "plan_delta": state.get("plan_delta", {}),
        "research_context": state.get("research_context", {}),
        "research_selection": state.get("research_selection", {}),
        "research_artifacts": list(state.get("research_artifacts") or [])[-6:],
        "disclaimer": _DISCLAIMER_EXPLORE,
    }
    # DD-13 §3.3/§5.1：探索版 reminders_preview = 起售提醒预览（presale 子集）
    payload["reminders_preview"] = reminders_preview(
        build_presale_reminders(state.get("transport_options") or {}, state.get("plan_id")))
    return payload


def compose_confirm_bundle(state: TripPlanState) -> dict:
    """确认版：探索版之上叠加回填、住宿、接驳、餐饮、时间线、校验；含§06 花费/风险/备选。"""
    from ..domain.compose import build_reminders, reminders_preview  # 惰性导入防循环

    c = state.get("constraints") or {}
    payload = {
        "version": "confirm",
        "plan_id": state.get("plan_id"),
        "theme": _theme(c, state.get("activities")),
        "time_windows": _time_windows(c),
        "budget_range": _budget_range(c),
        "cities": _overlay_primary_count(state.get("candidate_cities", []), state.get("activities")),
        "activities": state.get("activities", []),
        "research_outcome": state.get("research_outcome"),
        "transport": state.get("transport_options", {}),
        "bookings": state.get("bookings", []),
        "hotel_area": state.get("hotel_area", {}),
        "local_routes": state.get("local_routes", []),
        "dining": state.get("dining", []),
        "timeline": state.get("timeline", []),
        "validation": state.get("validation", {}),
        "cost": _cost_summary(state),
        "risks": _risks(state),
        "alternatives": _alternatives(state),
        "weather": state.get("weather", {}),
        "warnings": _current_warnings(state),
        "assistant_response": state.get("assistant_response"),
        "disclaimer": _DISCLAIMER_CONFIRM,
    }
    # DD-13 §3.3/§5.2：确认版 reminders_preview = 九类提醒可读摘要（§3.4 子集）
    payload["reminders_preview"] = reminders_preview(build_reminders(payload))
    return payload


# ============================ §06 字段派生（均标 estimated，诚实不编造）============================
_EV_EST = {"source_type": "rule", "verification_status": "estimated", "confidence": 0.4}

_RESEARCH_WARNING_PREFIXES = (
    "活动检索为空", "活动检索失败", "无候选城市", "本轮暂未找到更贴近反馈",
)


def _current_warnings(state: TripPlanState) -> list[str]:
    """去重告警；当前已有活动时剔除 reducer 留下的旧研究空态。"""
    warnings = list(dict.fromkeys(state.get("warnings", [])))
    if state.get("activities"):
        warnings = [
            warning for warning in warnings
            if not warning.startswith(_RESEARCH_WARNING_PREFIXES)
        ]
    return warnings


def _overlay_primary_count(cities: list, activities: list) -> list:
    """用实际入窗检索到的活动数刷新主城卡计数（消除 discover 早于深研的“0场”陈旧值）。"""
    if not cities:
        return cities
    out = [dict(x) for x in cities]
    n = len(activities or [])
    dba = dict(out[0].get("driven_by_activities") or {})
    dba["value"] = n
    evidence = dict(dba.get("evidence") or {})
    if evidence:
        evidence["note"] = f"最终入窗可信活动数 {n}"
        dba["evidence"] = evidence
    out[0]["driven_by_activities"] = dba
    reason = out[0].get("reason") or ""
    if re.search(r"当周\s*\d+\s*场", reason):
        out[0]["reason"] = re.sub(
            r"当周\s*\d+\s*场",
            f"当周 {n} 场",
            reason,
            count=1,
        )
    elif n and "待补搜" in reason:
        out[0]["reason"] = f"当周 {n} 场可选活动"
    return out


def _theme(c: dict, activities) -> str:
    requirements = c.get("experience_requirements") or c.get("interests") or []
    if requirements:
        return "·".join(str(value) for value in requirements[:3]) + "之旅"
    cats = [a.get("category") for a in (activities or []) if a.get("category")]
    return ("·".join(list(dict.fromkeys(cats))[:3]) + "之旅") if cats else "周末探索"


def _time_windows(c: dict) -> dict:
    return {
        "depart": c.get("earliest_depart") or c.get("weekend_start"),
        "return": c.get("latest_return") or c.get("weekend_end"),
        "evidence": _EV_EST,
    }


def _budget_range(c: dict) -> dict:
    bb = c.get("budget_band") or {}
    return {"min": bb.get("min"), "max": bb.get("max"), "per_person": True,
            "note": "以官方票价为准", "evidence": _EV_EST}


def _lodging_hint(state: TripPlanState) -> dict:
    ha = state.get("hotel_area") or {}
    if ha.get("name"):
        return {"name": ha["name"], "evidence": ha.get("evidence", _EV_EST)}
    cities = state.get("candidate_cities") or []
    nm = cities[0].get("name") if cities else None
    return {"name": None, "note": (f"{nm}市区（回填/确认后细化）" if nm else "待确认"), "evidence": _EV_EST}


def _pending_checklist(state: TripPlanState) -> list[str]:
    kinds = {b.get("kind") for b in (state.get("bookings") or []) if b.get("confirmed")}
    items: list[str] = []
    if "train" not in kinds and "flight" not in kinds:
        items.append("交通：车次/航班待回填确认")
    if "hotel" not in kinds:
        items.append("住宿：酒店待确认")
    if any((a.get("availability_status") or "") == "user_must_confirm" for a in (state.get("activities") or [])):
        items.append("活动：余票/票价请到官方页面确认")
    return items


def _cost_summary(state: TripPlanState) -> dict:
    """诚实：不编造票价。已确认花费仅在回填含价时汇总，否则标“见票面”。"""
    prices = [p for b in (state.get("bookings") or [])
              if isinstance((p := (b.get("extracted") or {}).get("price")), (int, float))]
    bb = (state.get("constraints") or {}).get("budget_band") or {}
    return {
        "confirmed": sum(prices) if prices else None,
        "confirmed_note": None if prices else "已确认项票面价见各自订单",
        "pending_estimate": {"max": bb.get("max"), "note": "预估待花，以官方票价为准"},
        "evidence": _EV_EST,
    }


def _risks(state: TripPlanState) -> list[dict]:
    risks: list[dict] = []
    cities = state.get("candidate_cities") or []
    if cities:
        sr = cities[0].get("risks") or {}
        val = sr.get("value") if isinstance(sr, dict) and "value" in sr else sr
        if val:
            risks.append({"type": "seasonal", "detail": val, "evidence": _EV_EST})
    w = state.get("weather") or {}
    if w.get("adverse"):
        risks.append({"type": "weather", "detail": w.get("detail") or "恶劣天气",
                      "advice": "户外项改室内优先", "evidence": w.get("evidence", _EV_EST)})
    for i in (state.get("validation") or {}).get("issues") or []:
        risks.append({"type": "validation", "detail": i, "evidence": _EV_EST})
    return risks


def _alternatives(state: TripPlanState) -> dict:
    alts: dict = {}
    cities = state.get("candidate_cities") or []
    if len(cities) > 1:
        alts["cities"] = [{"name": x.get("name"), "city_code": x.get("city_code")} for x in cities[1:3]]
    fb = [d for d in (state.get("dining") or []) if d.get("is_fallback")]
    if fb:
        alts["dining_fallback"] = fb[0].get("name")
    return alts
