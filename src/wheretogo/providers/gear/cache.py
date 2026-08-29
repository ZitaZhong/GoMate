"""分层 TTL 缓存（DD-04 §4.1）。

优先 Redis（跨进程共享，键走 infra.RedisKeys.cache(provider, req_key)）；
Redis 不可用 → 进程内 dict + 过期时间（离线可测）。值序列化为 JSON。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from ...infra.redis_client import RedisKeys, get_redis

_redis_ok: bool | None = None


def redis_available() -> bool:
    """惰性探测 Redis 是否在线（结果缓存，避免每次 ping）。"""
    global _redis_ok
    if _redis_ok is None:
        try:
            get_redis().ping()
            _redis_ok = True
        except Exception:
            _redis_ok = False
    return _redis_ok


class Cache:
    def __init__(self, redis: bool = True) -> None:
        self._use_redis = redis
        self._mem: dict[str, tuple[Any, float]] = {}
        self._lock = threading.Lock()

    def _full_key(self, provider: str, req_key: str) -> str:
        return RedisKeys.cache(provider, req_key)

    def get(self, provider: str, req_key: str) -> Any:
        key = self._full_key(provider, req_key)
        if self._use_redis and redis_available():
            try:
                raw = get_redis().get(key)
                if raw:
                    return json.loads(raw)
            except Exception:
                pass
        with self._lock:
            item = self._mem.get(req_key)
            if item and item[1] > time.monotonic():
                return item[0]
            if item:
                self._mem.pop(req_key, None)
        return None

    def set(self, provider: str, req_key: str, value: Any, ttl: int) -> None:
        if not value or ttl <= 0:
            return
        key = self._full_key(provider, req_key)
        payload = json.dumps(value, ensure_ascii=False, default=str)
        if self._use_redis and redis_available():
            try:
                get_redis().set(key, payload, ex=ttl)
                return
            except Exception:
                pass
        with self._lock:
            self._mem[req_key] = (value, time.monotonic() + ttl)
