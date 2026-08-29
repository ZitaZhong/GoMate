"""DD-04 统一 Provider 抽象（同步实现，匹配本仓库 sync 栈）。

所有领域模块通过本层访问外部世界，不得裸调外部 API（DD-04 §1 边界）。
`Result.source_type` 喂 DD-03 定级；`degraded=True` 表示走了降级路径。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol


@dataclass
class Req:
    """一次外部调用请求。"""

    op: str  # geocode/route/poi_search/weather_hourly/flight_schedule/web_search/chat/...
    params: dict
    cache_ttl: int = 0  # 秒；0 表示不缓存（见 gear 缓存分层 TTL）

    @property
    def key(self) -> str:
        """缓存键原料：op + 规范化 params 的 sha1。"""
        raw = f"{self.op}|{sorted(self.params.items(), key=lambda kv: kv[0])!r}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@dataclass
class Result:
    """外部调用结果。source_type 用于 DD-03 字段定级。"""

    ok: bool
    data: dict | None
    source_type: str  # amap/qweather/variflight/search/llm/unknown ...
    degraded: bool = False
    # Machine-readable failure metadata must survive fallback layers.  Without
    # it, "provider unavailable" becomes indistinguishable from a legitimate
    # empty search result.
    error: dict | None = None
    degraded_reason: str | None = None


class Provider(Protocol):
    """Provider 协议：同步 call。real 实现 httpx 调真 API；fallback 确定性兜底。"""

    name: str

    def call(self, req: Req) -> Result: ...


# —— 缓存分层 TTL（DD-04 §4.1，秒）——
TTL_BY_OP: dict[str, int] = {
    "geocode": 30 * 24 * 3600,
    "regeo": 30 * 24 * 3600,
    "route": 7 * 24 * 3600,
    "distance_matrix": 7 * 24 * 3600,
    "poi_search": 3 * 24 * 3600,
    "weather_hourly": 3600,
    "minutely_precip": 1800,
    "warning": 3600,
    "flight_schedule": 12 * 3600,
    "flight_status": 12 * 3600,
    "web_search": 24 * 3600,
    "web_search_deep": 24 * 3600,
    "chat": 0,  # 对话默认不缓存
    "extract": 24 * 3600,
    "ocr": 24 * 3600,
}
