"""VariFlight 航班 Provider（DD-04 §5）。

ops: flight_schedule / flight_status。
强约束：**只取时刻/机型/准点率，绝不取实时票价**（DD-03 闸三）。
real：VariFlight API（BYO WTG_VARIFLIGHT_KEY，端点可配）。fallback：按城市对直线距离估典型时长
（estimated），不产任何价格/余票字段。
"""
from __future__ import annotations

from ..config import get_settings
from ._geo import haversine_m
from ._net import get_json
from .base import Req, Result

_VARIFLIGHT_BASE = "https://api.variflight.com"


class VariFlightProvider:
    name = "variflight"

    def __init__(self) -> None:
        s = get_settings()
        self._key = s.variflight_key

    def call(self, req: Req) -> Result:
        if not self._key:
            return Result(ok=False, data=None, source_type="variflight")
        p = req.params
        try:
            data = get_json(
                f"{_VARIFLIGHT_BASE}/flight",
                params={"fnum": p.get("flight_no", ""), "date": p.get("date", ""),
                        "app_id": p.get("app_id", ""), "access_token": self._key},
            )
            flights = data.get("flight") or []
            # 只保留时刻/机型；显式剔除任何价格/余票字段（即便上游返回也不用）
            cleaned = [
                {k: f[k] for k in ("fnum", "flightCompanyName", "flightType", "depPlanTime",
                                   "arrPlanTime", "depAirport", "arrAirport") if k in f}
                for f in flights
            ]
            return Result(ok=True, data={"flights": cleaned}, source_type="variflight")
        except Exception:
            return Result(ok=False, data=None, source_type="variflight")


class VariFlightFallback:
    """按城市中心直线距离估典型航班时长（estimated）。不产价格/余票。"""

    name = "variflight"

    def call(self, req: Req) -> Result:
        p = req.params
        o = p.get("origin_coord")  # [lng,lat]
        d = p.get("dest_coord")
        if not (o and d):
            return Result(ok=False, data=None, source_type="unknown")
        dist_km = haversine_m((o[1], o[0]), (d[1], d[0])) / 1000.0
        # 巡航 ~800km/h + 起降/滑行 30min 开销
        flight_min = max(55, int(dist_km / 800.0 * 60) + 30) if dist_km > 200 else None
        if flight_min is None:
            return Result(ok=False, data=None, source_type="unknown")
        return Result(
            ok=True,
            data={"typical_duration_min": flight_min, "distance_km": int(dist_km),
                  "estimate": True, "note": "仅估时长，价格/余票请以航司官方为准"},
            source_type="variflight",
            degraded=True,
        )
