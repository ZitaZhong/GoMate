"""高德地图 Provider（DD-04 §5）。

ops: geocode / poi_search / route / distance_matrix。
real：高德 REST API（BYO WTG_AMAP_KEY）。fallback：直线估算（haversine）+ 高德深链，
source_type=amap、degraded=True；无法确定性求解（如地址→坐标无数据）时返回 unknown。
"""
from __future__ import annotations

from ..config import get_settings
from ._geo import estimate_minutes, haversine_m
from ._net import get_json
from .base import Req, Result

_AMAP_BASE = "https://restapi.amap.com/v3"
_DEEPLINK_BASE = "https://uri.amap.com/navigation"


def _parse_loc(s: str) -> list[float] | None:
    """高德 'lng,lat' → [lng, lat]（GeoJSON 顺序）。"""
    if not s or "," not in s:
        return None
    try:
        lng, lat = s.split(",")
        return [float(lng), float(lat)]
    except (ValueError, TypeError):
        return None


def _to_latlng(point: list[float]) -> tuple[float, float]:
    return point[1], point[0]


class AmapProvider:
    name = "amap"

    def __init__(self) -> None:
        self._key = get_settings().amap_key

    def call(self, req: Req) -> Result:
        if not self._key:
            return Result(ok=False, data=None, source_type="amap")
        op = req.op
        p = req.params
        try:
            if op == "geocode":
                data = get_json(
                    f"{_AMAP_BASE}/geocode/geo",
                    params={"address": p.get("address", ""), "city": p.get("city", ""),
                            "key": self._key},
                )
                gcs = data.get("geocodes") or []
                loc = _parse_loc(gcs[0]["location"]) if gcs else None
                return Result(ok=bool(loc), data={"location": loc}, source_type="amap")
            if op == "poi_search":
                data = get_json(
                    f"{_AMAP_BASE}/place/text",
                    params={"keywords": p.get("keyword", ""), "city": p.get("city", ""),
                            "citylimit": "true", "offset": p.get("limit", 10), "key": self._key},
                )
                pois = [
                    {"name": it.get("name"), "location": _parse_loc(it.get("location", "")),
                     "address": it.get("address"), "typecode": it.get("typecode")}
                    for it in (data.get("pois") or [])
                ]
                return Result(ok=True, data={"pois": pois}, source_type="amap")
            if op == "route":
                return self._route(p)
            if op == "distance_matrix":
                return self._matrix(p)
        except Exception:
            return Result(ok=False, data=None, source_type="amap")
        return Result(ok=False, data=None, source_type="amap")

    def _route(self, p: dict) -> Result:
        origin = p.get("origin")  # [lng,lat]
        dest = p.get("destination")
        mode = p.get("mode", "driving")
        endpoint = {"driving": "driving", "walk": "walking", "transit": "transit/integrated"}.get(mode, "driving")
        data = get_json(
            f"{_AMAP_BASE}/direction/{endpoint}",
            params={"origin": f"{origin[0]},{origin[1]}", "destination": f"{dest[0]},{dest[1]}",
                    "city": p.get("city", ""), "cityd": p.get("cityd", ""), "key": self._key},
        )
        route = data.get("route") or {}
        dist = int(route.get("distance", 0)) if route.get("distance") else None
        dur = int(int(route.get("duration", 0)) / 60) if route.get("duration") else None
        ok = dist is not None
        return Result(ok=ok, data={"distance_m": dist, "duration_min": dur, "mode": mode}, source_type="amap")

    def _matrix(self, p: dict) -> Result:
        dest = p["destination"]
        origins = p.get("origins") or []
        ostr = "|".join(f"{o[0]},{o[1]}" for o in origins)
        data = get_json(
            f"{_AMAP_BASE}/distance",
            params={"origins": ostr, "destination": f"{dest[0]},{dest[1]}",
                    "type": p.get("type", "1"), "key": self._key},
        )
        rows = [
            {"distance_m": int(it.get("distance", 0)), "duration_min": int(it.get("duration", 0)) // 60}
            for it in (data.get("results") or [])
        ]
        return Result(ok=True, data={"rows": rows}, source_type="amap")


class AmapFallback:
    """确定性兜底：route/distance_matrix 用 haversine 直线估算；geocode/poi 无数据→unknown。"""

    name = "amap"

    def call(self, req: Req) -> Result:
        p = req.params
        if req.op in ("route", "distance_matrix"):
            dest = p.get("destination")
            if req.op == "route":
                origin = p.get("origin")
                if origin and dest:
                    d = haversine_m(_to_latlng(origin), _to_latlng(dest))
                    return Result(
                        ok=True,
                        data={"distance_m": int(d), "duration_min": estimate_minutes(d, p.get("mode", "transit")),
                              "mode": p.get("mode", "transit"), "estimate": True,
                              "deeplink": _deeplink(origin, dest, p.get("mode", "driving"))},
                        source_type="amap",
                        degraded=True,
                    )
            else:  # distance_matrix
                origins = p.get("origins") or []
                rows = []
                for o in origins:
                    d = haversine_m(_to_latlng(o), _to_latlng(dest)) if dest else 0.0
                    rows.append({"distance_m": int(d),
                                 "duration_min": estimate_minutes(d, p.get("mode", "transit")),
                                 "estimate": True})
                return Result(ok=True, data={"rows": rows}, source_type="amap", degraded=True)
        # geocode / poi_search 无法确定性求解
        return Result(ok=False, data=None, source_type="unknown")


def _deeplink(origin: list[float], dest: list[float], mode: str) -> str:
    m = {"driving": "car", "walk": "walk", "transit": "bus"}.get(mode, "car")
    return (f"{_DEEPLINK_BASE}?from={origin[0]},{origin[1]}&to={dest[0]},{dest[1]}"
            f"&mode={m}&coordinate=wgs84&callnative=1")
