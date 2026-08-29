"""日成本配额（DD-04 §8）。

按 provider + 当日（UTC）计数；接近上限（默认 90%）信号 `near_limit` → ResilientProvider
提前降级（换备用/规则）。进程内实现；BYO Key 时调用方传 cost=0 不计入我方配额。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

_DEFAULT_DAILY_MAX = {
    "amap": 8000,
    "qweather": 4000,
    "variflight": 2000,
    "search": 2000,
    "web_search_deep": 2000,
    "llm": 20000,
}
_FALLBACK_MAX = 5000
_NEAR_LIMIT_RATIO = 0.9


class Quota:
    def __init__(self) -> None:
        self._counts: dict[str, dict[str, int]] = {}  # provider -> {yyyymmdd: count}
        self._limits = dict(_DEFAULT_DAILY_MAX)
        self._lock = threading.Lock()

    def set_limit(self, provider: str, daily_max: int) -> None:
        with self._lock:
            self._limits[provider] = daily_max

    def incr(self, provider: str, cost: int = 1) -> None:
        if cost <= 0:
            return
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        with self._lock:
            by_day = self._counts.setdefault(provider, {})
            by_day[day] = by_day.get(day, 0) + cost

    def used(self, provider: str) -> int:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        with self._lock:
            return self._counts.get(provider, {}).get(day, 0)

    def near_limit(self, provider: str) -> bool:
        limit = self._limits.get(provider, _FALLBACK_MAX)
        return self.used(provider) >= int(limit * _NEAR_LIMIT_RATIO)
