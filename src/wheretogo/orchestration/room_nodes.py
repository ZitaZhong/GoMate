"""DD-18 §3.3 RoomPlanGraph 节点：市内多人活动编排。

节点只读写 RoomState；活动检索复用 DD-05 + DD-17（scope=local）；路线复用 DD-04 AMap
（无 key → haversine 兜底 + 深链）；证据体系完全复用 DD-03（不产事实）。
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from sqlalchemy import text

from ..db import get_session
from ..providers import call as provider_call
from ..retrieval import RetrievalService, Weekend
from ..rooms.algorithms import compute_common_window, compute_gathering, rank_by_fairness

try:
    from langgraph.types import interrupt
except Exception:  # pragma: no cover - langgraph 版本兜底
    interrupt = None  # type: ignore

_RETRIEVAL = RetrievalService()

#: 城市名 → city_code 静态兜底（库内 city_playbook 优先）
_CITY_CODES = {"上海": "310000", "北京": "110000", "广州": "440100",
               "深圳": "440300", "杭州": "330100", "成都": "510100"}

#: 成员 transport_pref → AMap route mode / buffer mode
_ROUTE_MODE = {"walk": "walk", "drive": "driving", "transit": "transit"}
_BUFFER_MODE = {"walk": "walking", "drive": "driving", "transit": "transit"}


def _city_code(city: str) -> str:
    try:
        with get_session() as s:
            code = s.scalar(
                text("SELECT city_code FROM city_playbook WHERE name = :n LIMIT 1"),
                {"n": city},
            )
            if code:
                return str(code)
    except Exception:
        pass
    return _CITY_CODES.get(city, "310000")


def _day_window(activity_date: str) -> Weekend:
    """活动当日窗口（00:00 ~ 23:59 UTC；市内单日活动）。"""
    d = datetime.fromisoformat(activity_date).date()
    start = datetime.combine(d, time(0, 0), tzinfo=timezone.utc)
    return Weekend(start, start + timedelta(hours=23, minutes=59))


def _stream_writer():
    try:
        from langgraph.config import get_stream_writer
        return get_stream_writer()
    except Exception:
        return None


def _emit(writer, phase: str, message: str, found: int = 0) -> None:
    if writer:
        try:
            writer({"phase": phase, "message": message, "found": found})
        except Exception:
            pass


# ============================ 节点 ============================
def collect_members(state: dict) -> dict:
    """collect_members：计算共同时间窗；不足 2 小时给建议（不阻塞）。"""
    members = state.get("members") or []
    window = compute_common_window(members)
    warnings = []
    if window.get("feasible") is False:
        warnings.append(
            "共同时间窗不足 2 小时："
            + "；".join(window.get("suggestions") or ["建议调整时间"])
        )
    return {"common_time_window": window, "warnings": warnings}


def select_theme(state: dict) -> dict:
    """select_theme：主题已在 API 阶段确认（直选/投票/AI/转盘），这里透传并记录。"""
    theme = state.get("theme")
    if not theme:
        return {"warnings": ["主题未确认，按全品类调研"]}
    return {"theme": theme}


def research_activities(state: dict) -> dict:
    """research：DD-05 库内检索 + DD-17 深研（scope=local）；深研失败→库内降级。"""
    writer = _stream_writer()
    city = state.get("city") or "上海"
    code = state.get("city_code") or _city_code(city)
    wk = _day_window(state["activity_date"])
    theme = state.get("theme")
    members = state.get("members") or []
    interests = sorted(set().union(*[set(m.get("interests") or []) for m in members])
                       ) if members else []
    categories = [theme] if theme else interests
    constraints = {
        "query": f"{city} 周末 {theme or ' '.join(interests) or '活动'}",
        "interests": categories,
    }
    research_meta: dict = {}
    warnings: list[str] = []
    try:
        with get_session() as s:
            _emit(writer, "plan", f"正在为「{city} · {theme or '全品类'}」检索市内活动…")
            from ..providers import has_key
            from ..research import deep_research, needs_deep_research
            if needs_deep_research() and has_key("search"):
                _emit(writer, "search", "启动市内实时深度研究（scope=local）…")
                res = deep_research(
                    code, wk, categories,
                    nl_query=f"{city} 本周末 {theme or ''} 市内活动".strip(),
                    interests=interests or None,
                    plan_id=None, trigger="user_explicit",
                    on_progress=lambda pe: _emit(
                        writer, getattr(pe, "phase", "search"),
                        getattr(pe, "message", ""), getattr(pe, "found", 0)),
                    session=s, scope="local",
                )
                research_meta = {"job_id": res.job_id, "status": res.status,
                                 "degraded": res.degraded, "found": len(res.activity_ids)}
                if res.degraded:
                    warnings.append("实时深研降级，以下为库内已有活动，建议到官方渠道确认")
            else:
                research_meta = {"enabled": False}
                warnings.append("深研未启用（无 search key），使用库内活动，建议到官方渠道确认")
            cands = _RETRIEVAL.retrieve_activities(s, code, wk, constraints, top_k=20)
            acts = [_cand_dict(x) for x in cands]
    except Exception as e:  # 检索层异常 → 空候选 + 错误（不阻塞状态机）
        return {"activity_candidates": [], "research": {"error": str(e)},
                "errors": [{"node": "research_activities", "message": str(e)}],
                "warnings": ["活动检索失败，请稍后重试"]}
    _emit(writer, "ingest", f"候选活动 {len(acts)} 个", len(acts))
    if not acts:
        warnings.append("未找到符合条件的市内活动，可放宽主题或时间")
    return {"activity_candidates": acts, "research": research_meta, "warnings": warnings}


def _cand_dict(x) -> dict:
    return {
        "id": x.id, "title": x.title, "venue": x.venue, "category": x.category,
        "price_text": x.price_text, "booking_url": x.booking_url,
        "start_at": x.start_at.isoformat() if x.start_at else None,
        "end_at": x.end_at.isoformat() if x.end_at else None,
        "verification_status": x.verification_status,
        "location": list(x.location) if x.location else None,
        "evidence": x.evidence,
    }


def rank_activities(state: dict) -> dict:
    """rank：通勤矩阵（AMap distance_matrix，无 key→haversine 兜底）+ 综合排序。"""
    acts = state.get("activity_candidates") or []
    members = state.get("members") or []
    origins = [m.get("origin_coords") for m in members]
    origins_valid = [o for o in origins if o]
    matrix: dict[str, list[int]] = {}
    for act in acts:
        loc = act.get("location")
        if not loc or not origins_valid:
            continue
        res = provider_call("amap", "distance_matrix",
                            {"origins": origins_valid, "destination": loc,
                             "mode": "transit"})
        if res.ok and res.data:
            matrix[str(act["id"])] = [
                int(r.get("duration_min") or 0) for r in res.data.get("rows") or []]
    ranked = rank_by_fairness(acts, members, matrix, weather=state.get("weather"))
    return {"activity_candidates": ranked}


def confirm_activity(state: dict) -> dict:
    """confirm：interrupt 等用户/投票选定活动（DD-02 await_booking 同范式）。"""
    candidates = state.get("activity_candidates") or []
    selected = interrupt({
        "type": "select_activity",
        "candidates": candidates[:10],
        "common_time_window": state.get("common_time_window"),
    })
    if isinstance(selected, dict) and selected.get("id") is not None:
        chosen = next((a for a in candidates if str(a.get("id")) == str(selected["id"])),
                      selected)
    else:
        chosen = candidates[0] if candidates else {}
    return {"selected_activity": chosen, "status": "ACTIVITY_SELECTED"}


def plan_gathering(state: dict) -> dict:
    """gathering：每成员路线（AMap route/兜底）+ 集合点与倒推出发时间。"""
    activity = state.get("selected_activity") or {}
    members = state.get("members") or []
    dest = activity.get("location")
    routes: list[dict] = []
    warnings: list[str] = []
    for m in members:
        origin = m.get("origin_coords")
        pref = (m.get("transport_pref") or "transit")
        mode = _ROUTE_MODE.get(pref, "transit")
        route = {"member_id": m.get("member_id"), "nickname": m.get("nickname"),
                 "transport_mode": _BUFFER_MODE.get(pref, "transit"),
                 "duration_min": 30, "estimate": True}  # 无坐标 → 30min 保守估计
        if origin and dest:
            res = provider_call("amap", "route",
                                {"origin": origin, "destination": dest, "mode": mode})
            if res.ok and res.data:
                route.update({
                    "duration_min": int(res.data.get("duration_min") or 30),
                    "distance_m": res.data.get("distance_m"),
                    "estimate": bool(res.data.get("estimate")),
                    "deeplink": res.data.get("deeplink"),
                })
                if res.degraded:
                    route["note"] = "直线估算，建议在地图 App 确认"
            else:
                warnings.append(f"{m.get('nickname')} 的路线计算失败，展示估算值")
        elif not origin:
            warnings.append(f"{m.get('nickname')} 未填写出发地，使用默认 30 分钟估算")
        routes.append(route)
    # 活动无开始时间，或开始日 ≠ 房间活动日（长展/常设活动只有开幕日）
    # → 按活动日 + 共同时间窗估算集合时间（estimated，不臆测场次）
    start_iso = activity.get("start_at")
    start_mismatch = True
    if start_iso:
        try:
            start_mismatch = (
                datetime.fromisoformat(start_iso).date().isoformat()
                != str(state["activity_date"])
            )
        except (ValueError, TypeError):
            start_mismatch = True
    if start_mismatch:
        cw = state.get("common_time_window") or {}
        start = cw.get("start") or "14:00"
        new_start = f"{state['activity_date']}T{start}:00"
        patched = {**activity, "start_at": new_start, "start_estimated": True}
        # end_at 同样不能透传展期结束日（否则节点时长=整个展期，实测出现 42450 分钟）：
        # 仅当原 end 恰好落在活动日当天（真实当日场次）才保留；否则按 start+3h 估算，
        # 且不晚于共同时间窗结束，标 end_estimated（不臆测"展期=游览时长"）。
        end_iso = activity.get("end_at")
        end_is_same_day = False
        if end_iso:
            try:
                end_is_same_day = (
                    datetime.fromisoformat(end_iso).date().isoformat()
                    == str(state["activity_date"])
                )
            except (ValueError, TypeError):
                end_is_same_day = False
        if not end_is_same_day:
            end_dt = datetime.fromisoformat(new_start) + timedelta(hours=3)
            cw_end = cw.get("end")
            if cw_end:
                try:
                    end_dt = min(
                        end_dt,
                        datetime.fromisoformat(f"{state['activity_date']}T{cw_end}:00"),
                    )
                except (ValueError, TypeError):
                    pass
            patched["end_at"] = end_dt.isoformat()
            patched["end_estimated"] = True
        activity = patched
        warnings.append("活动当日具体场次未核实，集合时间按共同时间窗估算，建议到官方确认")
    gathering = compute_gathering(activity, members, routes)
    return {"gathering": gathering, "member_routes": routes,
            "selected_activity": activity, "warnings": warnings}


def generate_itinerary(state: dict) -> dict:
    """itinerary：时间线编排（集合 → 活动 → 顺路餐饮建议）。"""
    activity = state.get("selected_activity") or {}
    gathering = state.get("gathering") or {}
    nodes: list[dict] = []
    gp = gathering.get("gathering_point") or {}
    if gathering.get("target_time"):
        nodes.append({
            "type": "gathering",
            "title": f"集合 · {gp.get('name', '活动地点')}",
            "start": gathering["target_time"],
            "point_type": gp.get("type"),
            "evidence": {"verification_status": "estimated"},
        })
    nodes.append({
        "type": "activity",
        "title": activity.get("title") or "市内活动",
        "venue": activity.get("venue"),
        "location": activity.get("location"),
        "start": activity.get("start_at"),
        "end": activity.get("end_at"),
        "booking_url": activity.get("booking_url"),
        "evidence": activity.get("evidence")
        or {"verification_status": activity.get("verification_status", "unknown")},
    })
    dining = _dining_suggestion(state, activity)
    if dining:
        nodes.append(dining)
    itinerary = {
        "room_id": state.get("room_id"),
        "theme": state.get("theme"),
        "activity_date": state.get("activity_date"),
        "nodes": nodes,
        "gathering": gathering,
        "member_routes": state.get("member_routes") or [],
        "common_time_window": state.get("common_time_window"),
        "warnings": list(state.get("warnings") or []),
    }
    return {"itinerary": itinerary}


def _dining_suggestion(state: dict, activity: dict) -> dict | None:
    """顺路餐饮建议（复用 DD-11 检索；失败→不加节点，不臆测）。"""
    loc = activity.get("location")
    if not loc:
        return None
    cw = state.get("common_time_window") or {}
    end = cw.get("end") or "21:00"
    meal_slot = "dinner" if end >= "19:00" else "lunch"
    dietary = sorted(set().union(
        *[set(m.get("negative_prefs") or []) for m in (state.get("members") or [])]
    )) if state.get("members") else []
    try:
        picks = _RETRIEVAL.retrieve_dining(
            (loc[0], loc[1]), meal_slot, {"dietary": dietary}, top_k=1)
        if picks:
            p = picks[0]
            return {
                "type": "dining",
                "title": p.name,
                "meal_slot": meal_slot,
                "evidence": p.evidence or {"verification_status": "estimated"},
            }
    except Exception:
        pass
    return None


def publish(state: dict) -> dict:
    """publish：行程落库（room_itineraries v1）+ 房间进入 PUBLISHED。"""
    from ..models import Room
    from ..rooms.service import save_itinerary_version

    itinerary = state.get("itinerary") or {}
    version = 0
    try:
        with get_session() as s:
            version = save_itinerary_version(s, int(state["room_id"]), itinerary)
            room = s.get(Room, int(state["room_id"]))
            if room:
                room.status = "PUBLISHED"
    except Exception as e:
        return {"itinerary_version": 0, "status": "PLANNING",
                "errors": [{"node": "publish", "message": str(e)}]}
    return {"itinerary_version": version, "status": "PUBLISHED"}
