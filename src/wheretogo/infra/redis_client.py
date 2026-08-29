"""Redis 客户端与键空间（DD-01 §9.1）。

键模式：
  cache:{provider}:{sha1(req)}   外部 API 响应缓存（分层 TTL）
  rl:{provider}                  令牌桶限流
  quota:{provider}:{yyyymmdd}    日成本计数
  lock:plan:{id}                 同一 plan 串行恢复（DD-02）
  invite:{token}                 邀请令牌 -> plan_id
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from threading import Lock

import redis

from ..config import get_settings

_local_plan_locks: dict[str, Lock] = {}
_local_plan_locks_guard = Lock()


def _local_plan_lock(plan_id: str | int) -> Lock:
    key = str(plan_id)
    with _local_plan_locks_guard:
        return _local_plan_locks.setdefault(key, Lock())


@lru_cache
def get_redis() -> redis.Redis:
    """进程级共享 Redis 客户端（指向隔离实例，端口 6380）。"""
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


class RedisKeys:
    """键构造器（集中管理，避免键名散落）。"""

    #: 缓存分层 TTL（秒）—— DD-01 §9.1
    CACHE_TTL = {
        "geocode": 30 * 24 * 3600,
        "route": 7 * 24 * 3600,
        "weather": 3600,
        "activity_search": 600,
        "llm": 24 * 3600,
    }

    @staticmethod
    def cache(provider: str, req: str) -> str:
        digest = hashlib.sha1(req.encode("utf-8")).hexdigest()
        return f"cache:{provider}:{digest}"

    @staticmethod
    def rate_limit(provider: str) -> str:
        return f"rl:{provider}"

    @staticmethod
    def quota(provider: str, day: datetime | None = None) -> str:
        d = (day or datetime.now(timezone.utc)).strftime("%Y%m%d")
        return f"quota:{provider}:{d}"

    @staticmethod
    def plan_lock(plan_id: str | int) -> str:
        return f"lock:plan:{plan_id}"

    @staticmethod
    def invite(token: str) -> str:
        return f"invite:{token}"


@contextmanager
def plan_lock(plan_id: str | int, timeout: int = 900, blocking_timeout: int = 10) -> Iterator[bool]:
    """同一 plan 的 resume/replan 串行锁（DD-02 §9：避免并发写 checkpoint）。

    锁 TTL 覆盖深度研究的最长正常耗时；拿不到锁抛异常，调用方应返回冲突。
    Redis 不可用 → 仍保留进程内锁，避免单实例并发写坏状态。
    """
    local = _local_plan_lock(plan_id)
    if not local.acquire(timeout=max(0, blocking_timeout)):
        raise TimeoutError(f"无法获取本地 plan 锁：{plan_id}（可能有并发操作）")
    try:
        r = get_redis()
        try:
            r.ping()
        except Exception:
            yield False  # Redis 不可用 → 至少保留单进程锁
            return
        lock = r.lock(RedisKeys.plan_lock(plan_id), timeout=timeout, blocking_timeout=blocking_timeout)
        acquired = lock.acquire()
        if not acquired:
            raise TimeoutError(f"无法获取 plan 锁：{plan_id}（可能有并发恢复/重规划在进行）")
        try:
            yield True
        finally:
            try:
                lock.release()
            except redis.exceptions.LockError:
                pass  # 锁已过期，忽略
    finally:
        local.release()
