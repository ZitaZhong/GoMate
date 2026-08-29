"""DD-11 住宿/市内交通/餐饮（hotel/mobility/dining 节点领域实现）。

- hotel：先区域后酒店（回填酒店优先）。
- mobility：逐段接驳——高德 route（无 key→直线估算）；坐标缺失则标注未估，绝不写死等差。
- dining：真实来源优先（高德 POI→Tavily 搜索），按动线契合+偏好+忌讳重排，永远留一个稳妥
  备选；无任何可信来源→空态 + 诚实提示，绝不编造假餐厅。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..providers import call as provider_call, extract_json, has_key
from ..retrieval import RetrievalService


def _ev_city(note: str | None = None) -> dict:
    return {"source_type": "open_dataset", "verification_status": "public_source_observed",
            "confidence": 0.7, "note": note}


def _ev_est(note: str | None = None) -> dict:
    return {"source_type": "rule", "verification_status": "estimated", "confidence": 0.4, "note": note}


def _ev_rule(note: str | None = None) -> dict:
    return {"source_type": "rule", "verification_status": "public_source_observed", "confidence": 0.6, "note": note}


def _coord_tuple(v) -> tuple[float, float] | None:
    """dict{lng,lat} / [lng,lat] / (lng,lat) → (lng, lat)；无效→None。"""
    if not v:
        return None
    if isinstance(v, dict):
        lng, lat = v.get("lng"), v.get("lat")
        return (float(lng), float(lat)) if lng is not None and lat is not None else None
    if isinstance(v, (list, tuple)) and len(v) == 2 and v[0] is not None:
        return (float(v[0]), float(v[1]))
    return None


def plan_hotel_area(state: dict, session: Session) -> dict:
    """回填酒店优先；否则城市档案住宿区。"""
    booked = [b for b in (state.get("bookings") or []) if b.get("kind") == "hotel"]
    if booked:
        return {"source": "booking", "detail": booked[0].get("extracted", {}),
                "evidence": booked[0].get("evidence") or _ev_rule("用户回填酒店")}
    code = (state.get("candidate_cities") or [{}])[0].get("city_code", "310000")
    row = session.execute(text(
        "SELECT lodging_areas, ST_X(center::geometry) lng, ST_Y(center::geometry) lat "
        "FROM city_playbook WHERE city_code = :c"
    ), {"c": code}).first()
    areas = (row[0] if row else None) or []
    area = areas[0] if areas else {"name": "市中心"}
    center = area.get("center") or (
        {"lng": float(row[1]), "lat": float(row[2])} if row and row[1] is not None else None
    )
    return {"name": area.get("name", "市中心"), "center": center, "evidence": _ev_city("城市档案住宿区")}


def plan_local_mobility(activities: list[dict], hotel: dict) -> list[dict]:
    """逐段接驳：高德 route（无 key→直线估算，标 estimated）；坐标缺失→minutes=None 并注明。

    不再使用 20+i*5 等差假值：有坐标才给真实估算，没坐标就诚实标“未估”。
    """
    from .transport import _access

    acts = (activities or [])[:3]
    legs: list[dict] = []
    prev_ct = _coord_tuple((hotel or {}).get("center"))
    prev_label = (hotel or {}).get("name", "住宿区")
    for i, a in enumerate(acts):
        to_ct = _coord_tuple(a.get("location"))
        minutes = None
        note = "坐标缺失，接驳耗时未估（可用地图链接查看）"
        if prev_ct and to_ct:
            minutes, _ = _access(prev_ct, to_ct, "transit")
            note = "接驳耗时：高德路线/直线估算，精确以地图为准"
        legs.append({
            "seq": i, "from_label": prev_label, "to_label": a.get("title"),
            "mode": "transit", "minutes": minutes, "evidence": _ev_est(note),
        })
        prev_ct = to_ct or prev_ct
        prev_label = a.get("title")
    return legs


def _dining_from_amap(city_code: str) -> list[dict]:
    """高德 POI 搜索餐厅（有 amap key 时）。返回带坐标的真实 POI。"""
    res = provider_call("amap", "poi_search", {"keyword": "餐厅", "city": city_code, "limit": 8})
    if not (res.ok and res.data and res.data.get("pois")):
        return []
    pois: list[dict] = []
    for it in res.data["pois"]:
        name = it.get("name")
        if not name:
            continue
        loc = it.get("location")
        pois.append({
            "name": name, "cuisine": None,
            "location": tuple(loc) if isinstance(loc, (list, tuple)) and len(loc) == 2 else None,
            "evidence": {"source_type": "amap", "verification_status": "public_source_observed",
                         "confidence": 0.7, "note": "高德 POI"},
        })
    return pois


def _dining_from_search(city_name: str, area_name: str | None) -> list[dict]:
    """Tavily 搜索真实餐厅（无 amap key 时）。LLM 从正文抽取店名；无 LLM→空（不编造）。"""
    q = f"{city_name}{area_name or ''} 餐厅推荐 好吃 附近"
    res = provider_call("search", "web_search", {"query": q, "count": 6})
    if not (res.ok and res.data and res.data.get("results")):
        return []
    results = res.data["results"]
    corpus = "\n\n".join((r.get("content") or r.get("snippet") or "")[:1500] for r in results[:4])
    src_url = next((r.get("url") for r in results if r.get("url")), None)
    parsed = extract_json(
        "dining_extract",
        "从网页正文中提取真实存在的餐厅名称（不要编造/不要输出榜单标题），"
        "输出 JSON {restaurants:[{name,cuisine}]}，最多6个。",
        corpus,
    )
    items = (parsed or {}).get("restaurants") if isinstance(parsed, dict) else None
    pois: list[dict] = []
    for it in (items or [])[:6]:
        name = (it.get("name") if isinstance(it, dict) else it) or ""
        name = str(name).strip()
        if not name:
            continue
        cuisine = it.get("cuisine") if isinstance(it, dict) else None
        pois.append({
            "name": name, "cuisine": cuisine, "location": None,
            "evidence": {"source_type": "search", "verification_status": "public_source_observed",
                         "confidence": 0.55, "source_url": src_url, "note": "网络搜索抽取，未逐一核实"},
        })
    return pois


def _collect_dining_pois(city_code: str, city_name: str, area_name: str | None) -> list[dict]:
    """真实数据优先：有 amap→高德；否则有 search→Tavily；都没有→空（绝不写死假餐厅）。"""
    if has_key("amap"):
        pois = _dining_from_amap(city_code)
        if pois:
            return pois
    if has_key("search"):
        pois = _dining_from_search(city_name, area_name)
        if pois:
            return pois
    return []


def plan_dining(state: dict, hotel: dict, svc: RetrievalService, session: Session) -> list[dict]:
    """按动线契合+偏好+忌讳重排；来源为真实 POI（高德/搜索）。无来源→空态（诚实）。"""
    c = state["constraints"]
    code = (state.get("candidate_cities") or [{}])[0].get("city_code", "310000")
    ct = _coord_tuple((hotel or {}).get("center"))
    if not ct:
        row = session.execute(text(
            "SELECT ST_X(center::geometry), ST_Y(center::geometry) FROM city_playbook WHERE city_code = :c"
        ), {"c": code}).first()
        ct = (float(row[0]), float(row[1])) if row and row[0] is not None else None
    if not ct:
        return []
    city_name = session.scalar(
        text("SELECT name FROM city_playbook WHERE city_code = :c"), {"c": code}) or code
    pois = _collect_dining_pois(code, city_name, (hotel or {}).get("name"))
    if not pois:
        return []
    pois[-1]["is_fallback"] = True  # 保留一个稳妥备选（真实 POI，非编造）
    cands = svc.retrieve_dining(ct, "lunch", c, pois=pois, top_k=3)
    return [{"name": x.name, "cuisine": x.cuisine, "distance_m": x.distance_m,
             "is_fallback": x.is_fallback, "evidence": x.evidence} for x in cands]
