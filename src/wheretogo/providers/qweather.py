"""和风天气 Provider（DD-04 §5）。

ops: weather_hourly / minutely_precip / warning。
real：和风天气 API（BYO WTG_QWEATHER_KEY）。fallback：天气无法确定性猜测 → unknown
（domain 不触发天气重规划，符合"无数据不臆测"）。
"""
from __future__ import annotations

from ..config import get_settings
from ._net import get_json
from .base import Req, Result

_QWEATHER_BASE = "https://devapi.qweather.com"
_PATHS = {
    "weather_hourly": "/v7/weather/24h",
    "minutely_precip": "/v7/minutely/5m",
    "warning": "/v7/warning/now",
}


class QWeatherProvider:
    name = "qweather"

    def __init__(self) -> None:
        self._key = get_settings().qweather_key

    def call(self, req: Req) -> Result:
        if not self._key or req.op not in _PATHS:
            return Result(ok=False, data=None, source_type="qweather")
        loc = req.params.get("location")  # "lng,lat"
        try:
            data = get_json(
                f"{_QWEATHER_BASE}{_PATHS[req.op]}",
                params={"location": loc, "key": self._key, "lang": "zh"},
            )
            if str(data.get("code")) != "200":
                return Result(ok=False, data=None, source_type="qweather")
            return Result(ok=True, data={"hourly": data.get("hourly"),
                                         "minutely": data.get("minutely"),
                                         "warning": data.get("warning") or data.get("warningList")},
                          source_type="qweather")
        except Exception:
            return Result(ok=False, data=None, source_type="qweather")


class QWeatherFallback:
    """天气不可确定性求解；返回 unknown（domain 不触发天气重规划）。"""

    name = "qweather"

    def call(self, req: Req) -> Result:
        return Result(ok=False, data=None, source_type="unknown")
