"""DD-09 交通决策（transport 节点领域实现）。

确定性门到门引擎（接驳+缓冲+运行+有效游玩）+ 铁路/航班策略卡 + 12306 起售时间 + 官方深链 +
预填清单。**铁律：禁编票价/余票**（一律占位「以官方平台为准」）；缓冲=estimated；时刻来自静态/典型值。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from ..providers import call as provider_call
from ..providers._geo import estimate_minutes, haversine_m

# DD-09 §5.1 缓冲常量（分钟）；全部 source='rule' → estimated
BUFFER_RULES_MIN: dict[str, dict[str, int]] = {
    "rail": {"ingress": 25, "egress": 15},
    "air": {"checkin_with_bag": 90, "checkin_no_bag": 60, "egress_with_bag": 35, "egress_no_bag": 20},
}
_PRESALE_LEAD = timedelta(days=14)  # 预售期 15 天含当天 → 起售日 = 乘车日 - 14 天
_RAIL_TYPICAL_KMH = 200.0  # 高铁典型时速（粗估运行段，无实时时刻）
_AIR_TYPICAL_KMH = 800.0


def _est(note: str | None = None, src: str = "rule") -> dict:
    return {"source_type": src, "verification_status": "estimated", "confidence": 0.4, "note": note}


def _rule(note: str | None = None) -> dict:
    return {"source_type": "rule", "verification_status": "public_source_observed", "confidence": 0.6, "note": note}


def _access(a, b, mode: str = "transit") -> tuple[int, str]:
    """市内接驳分钟（高德 route；无 key→provider 自降级直线估算）。返回 (minutes, source)。"""
    if not (a and b):
        return 0, "rule"
    res = provider_call("amap", "route", {"origin": list(a), "destination": list(b), "mode": mode})
    if res.ok and res.data and res.data.get("duration_min") is not None:
        return int(res.data["duration_min"]), ("amap" if not res.degraded else "rule")
    dist = haversine_m((a[1], a[0]), (b[1], b[0]))
    return (estimate_minutes(dist, mode) or 0), "rule"


def _typical_run_min(origin, dest, mode: str) -> tuple[int, str]:
    """运行段典型时长（无实时时刻；rail=直线/时速，air=variflight 典型或直线估）。"""
    if not (origin and dest):
        return 0, "rule"
    if mode == "air":
        res = provider_call("variflight", "flight_schedule",
                            {"origin_coord": list(origin), "dest_coord": list(dest)})
        if res.ok and res.data and res.data.get("typical_duration_min"):
            return int(res.data["typical_duration_min"]), "variflight"
        dist_km = haversine_m((origin[1], origin[0]), (dest[1], dest[0])) / 1000.0
        return (int(dist_km / _AIR_TYPICAL_KMH * 60) + 30), "rule"
    dist_km = haversine_m((origin[1], origin[0]), (dest[1], dest[0])) / 1000.0
    return int(dist_km / _RAIL_TYPICAL_KMH * 60), "rule"


def estimate_door_to_door(origin, dest, mode: str = "rail") -> dict:
    """粗估档（供 DD-08 打分）：直线近似接驳 + 典型运行 + 缓冲。"""
    if not (origin and dest):
        return {"total_min": 0, "effective_play_min": 0, "mode": mode, "evidence": _est("缺少坐标，门到门未估")}
    run_min, _ = _typical_run_min(origin, dest, mode)
    buf = BUFFER_RULES_MIN["rail" if mode == "rail" else "air"]
    ingress = buf.get("ingress") or buf.get("checkin_with_bag", 90)
    egress = buf.get("egress") or buf.get("egress_with_bag", 35)
    access = 30  # 粗估档两端市内接驳合计
    total = access + ingress + run_min + egress
    effective_play = max(0, 32 * 60 - 2 * total)  # 一个周末约 32h 可支配
    return {"total_min": total, "run_min": run_min, "effective_play_min": effective_play,
            "mode": mode, "evidence": _est("门到门粗估")}


def door_to_door(origin, dest, mode: str = "rail", hotel=None, with_bag: bool = True) -> dict:
    """精算门到门：access_out + ingress + run + egress + access_in；逐段带 source。

    v0.1 运行段同粗估（无实时时刻）；接驳用城市中心近似（精算档预留接口）。
    """
    run_min, src_run = _typical_run_min(origin, dest, mode)
    buf = BUFFER_RULES_MIN["rail" if mode == "rail" else "air"]
    if mode == "rail":
        ingress, egress = buf["ingress"], buf["egress"]
    else:
        ingress = buf["checkin_with_bag"] if with_bag else buf["checkin_no_bag"]
        egress = buf["egress_with_bag"] if with_bag else buf["egress_no_bag"]
    total = ingress + run_min + egress
    return {
        "mode": mode, "total_min": total, "run_min": run_min,
        "buffer": {"ingress": ingress, "egress": egress},
        "effective_play_min": max(0, 32 * 60 - 2 * total),
        "evidence_by_seg": {
            "buffer_in": _est("进站/值机缓冲", "rule"),
            "run": _est(f"典型运行 {run_min}min（无实时时刻）", src_run),
            "buffer_out": _est("出站缓冲", "rule"),
        },
    }


def presale_open_time(travel_date: date, station_open_time: str = "08:00") -> datetime:
    """起售日 = 乘车日 - 14 天；时点 = 该站起售时点。结果标 estimated（以 12306 当前页面为准）。"""
    hh, mm = (int(x) for x in station_open_time.split(":"))
    return datetime.combine(travel_date - _PRESALE_LEAD, time(hh, mm))


def build_12306_entry(origin_name: str, dest_name: str, travel_date: date) -> dict:
    """12306 官方入口深链（诚实取舍：只给入口 + 预填日期，不承诺参数式直达某车次）。"""
    return {
        "url": "https://www.12306.cn/index/",
        "prefill_hint": {"from": origin_name, "to": dest_name, "date": travel_date.isoformat()},
        "disclaimer": "请以 12306 当前页面为准；本系统不代购、不查实时余票",
        "evidence": _rule("12306 官方入口"),
    }


def build_prefill_hints(constraints: dict, dest_name: str) -> dict:
    """供 await_booking interrupt payload 的回填预填清单（无票价/余票）。"""
    origins = constraints.get("origins") or []
    return {"rail": {"from": origins[0] if origins else "", "to": dest_name},
            "flight": {"from": origins[0] if origins else "", "to": dest_name}}


def _same_city(constraints: dict, origin: dict | None = None) -> bool:
    """同城判定：按出发城市码 vs 目标城市码（C4：去除 "上海" 硬编码）。

    origin 由 DD-08 解析（origins 子串匹配 city_playbook，默认上海）。无 origin 且无 origins
    时兼容视作同城（discover 未跑的兜底场景）。目的地留空（DD-07 不再静默默认城市）时
    按出发地同城处理——与 discover 的"留空 → 出发地同城推荐"保持一致。
    """
    target = constraints.get("target_city_code") or (origin or {}).get("city_code") or "310000"
    oc = (origin or {}).get("city_code")
    if oc:
        return oc == target
    origins = constraints.get("origins") or []
    return not origins


def _weekend_start_date(constraints: dict) -> date | None:
    """乘车日：优先 weekend_start，其次 earliest_depart（DD-01 标准字段，修 N2）。"""
    for key in ("weekend_start", "earliest_depart"):
        v = constraints.get(key)
        if v:
            try:
                return datetime.fromisoformat(v).date()
            except (ValueError, TypeError):
                continue
    return None


def decide_mode(d2d_rail: dict, d2d_air: dict, distance_km: float | None) -> str:
    """距离带 + 门到门时长判断推荐方式（PRD §5.4-A）。返回 rail/air/compare（不再恒 compare）。"""
    rail_min = d2d_rail.get("total_min") or 0
    air_min = d2d_air.get("total_min") or 0
    if distance_km is not None:
        if distance_km < 300:
            return "rail"
        if distance_km > 1000:
            return "air" if (air_min and rail_min and air_min < rail_min) else "compare"
    if rail_min and air_min:
        if rail_min <= air_min * 0.85:
            return "rail"
        if air_min <= rail_min * 0.85:
            return "air"
    return "compare"


def build_transport_options(constraints: dict, candidate_cities: list[dict],
                            origin: dict | None = None) -> dict:
    """装配 transport_options DTO（DD-09 §3.2）。prefill/presale 在顶层（供 await_booking 读）。"""
    if _same_city(constraints, origin):
        same_code = (constraints.get("target_city_code")
                     or (origin or {}).get("city_code") or "310000")
        same_name = ((candidate_cities or [{}])[0].get("name")
                     or (origin or {}).get("name") or same_code)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "origin": (origin or {}).get("name") or (constraints.get("origins") or ["同城"])[0],
            "candidates": [{"city": same_name, "city_code": same_code,
                            "recommended_mode": "local", "reason": "同城/近郊出行，无需城际交通"}],
            "prefill": {}, "presale": [],
            "disclaimer": "同城出行，无需城际交通", "degraded": False,
        }

    dest = candidate_cities[0] if candidate_cities else {
        "name": constraints.get("target_city_code"), "city_code": constraints.get("target_city_code")}
    dest_name = dest.get("name") or dest.get("city_code")
    dest_center = dest.get("center")
    origin_center = (origin or {}).get("center")
    d2d_rail = estimate_door_to_door(origin_center, dest_center, "rail")
    d2d_air = estimate_door_to_door(origin_center, dest_center, "air")
    dist_km = (haversine_m((origin_center[1], origin_center[0]), (dest_center[1], dest_center[0])) / 1000.0
               if origin_center and dest_center else None)
    mode = decide_mode(d2d_rail, d2d_air, dist_km) if dist_km is not None else "compare"
    reason = {"rail": "城际出行：推荐高铁（中短途/门到门更优）",
              "air": "城际出行：推荐航班（长途/门到门更优）",
              "compare": "城际出行：建议比较高铁与航班门到门"}[mode]
    travel_date = _weekend_start_date(constraints)
    origin_name = (origin or {}).get("name") or (constraints.get("origins") or ["出发地"])[0]
    presale: list[dict] = []
    if travel_date:
        open_at = presale_open_time(travel_date)
        presale.append({
            "city": dest_name, "route": f"{origin_name} → {dest_name}",
            "open_at": open_at.isoformat(), "station": "始发站",
            # 起售时点已过 → 不再提醒"等待起售"，文案改为已起售（诚实状态，不催过期动作）
            "note": "已起售，请直接购买" if open_at <= datetime.now() else "未到起售时间，到点可购",
            "disclaimer": "起售时间以 12306 当前页面为准", "evidence": _est("起售提醒（规则计算）"),
        })
    entry = build_12306_entry(origin_name, dest_name, travel_date or date.today())
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "origin": origin_name,
        "candidates": [{
            "city": dest_name, "city_code": dest.get("city_code"),
            "recommended_mode": mode,
            "reason": reason,
            "door_to_door": {"rail": d2d_rail, "air": d2d_air},
            "official_entry": entry,
            "disclaimer": "票价/余票以官方平台为准；门到门缓冲为估算值",
        }],
        "prefill": build_prefill_hints(constraints, dest_name),
        "presale": presale,
        "disclaimer": "票价/余票以官方平台为准；本系统不编造", "degraded": False,
    }
