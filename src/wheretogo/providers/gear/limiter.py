"""令牌桶限流（DD-04 §4.2）。

进程内令牌桶（按 provider）。Redis Lua 原子为未来跨进程增强；v0.1 单进程内已足够满足
DoD「压测触发限流→自动降级不报错」。
"""
from __future__ import annotations

import threading
import time


class _Bucket:
    __slots__ = ("rate", "capacity", "tokens", "updated", "lock")

    def __init__(self, rate: float, capacity: float) -> None:
        self.rate = rate  # tokens / sec
        self.capacity = capacity
        self.tokens = capacity
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def allow(self) -> bool:
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False


class Limiter:
    """按 provider 维护独立令牌桶。"""

    def __init__(self, default_rate: float = 8.0, default_capacity: float = 8.0) -> None:
        self.default_rate = default_rate
        self.default_capacity = default_capacity
        self._buckets: dict[str, _Bucket] = {}
        self._overrides: dict[str, tuple[float, float]] = {}
        self._guard = threading.Lock()

    def configure(self, provider: str, rate: float, capacity: float) -> None:
        with self._guard:
            self._overrides[provider] = (rate, capacity)
            self._buckets.pop(provider, None)

    def allow(self, provider: str) -> bool:
        with self._guard:
            bucket = self._buckets.get(provider)
            if bucket is None:
                rate, cap = self._overrides.get(
                    provider, (self.default_rate, self.default_capacity)
                )
                bucket = _Bucket(rate, cap)
                self._buckets[provider] = bucket
        return bucket.allow()
