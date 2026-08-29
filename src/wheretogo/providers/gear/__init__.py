"""DD-04 五件套底座：缓存 / 限流 / 熔断 / 配额。

全部组件在 Redis 不可用时退化为进程内实现（离线/单测可跑）；Redis 在线则跨进程共享。
"""
from __future__ import annotations

from .breaker import CircuitBreaker
from .cache import Cache
from .limiter import Limiter
from .quota import Quota

__all__ = ["Cache", "Limiter", "CircuitBreaker", "Quota"]
