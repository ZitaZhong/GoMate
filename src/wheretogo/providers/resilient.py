"""ResilientProvider：缓存→配额→限流→熔断→主调→降级链→unknown（DD-04 §3.1，同步版）。

任一外部单点失效最差返回 `Result(ok=False/True, source_type="unknown", degraded=True)`，
交给 DD-03 标 `unknown`，绝不空结果、绝不编造。
"""
from __future__ import annotations

from .base import Req, Result
from .gear import Cache, CircuitBreaker, Limiter, Quota


class ResilientProvider:
    def __init__(
        self,
        primary,
        fallbacks: list | None = None,
        *,
        cache: Cache | None = None,
        limiter: Limiter | None = None,
        breaker: CircuitBreaker | None = None,
        quota: Quota | None = None,
    ) -> None:
        self.primary = primary
        self.fallbacks = list(fallbacks or [])
        self.cache = cache or Cache()
        self.limiter = limiter or Limiter()
        self.breaker = breaker or CircuitBreaker()
        self.quota = quota or Quota()

    @property
    def name(self) -> str:
        return self.primary.name

    def call(self, req: Req) -> Result:
        # ① 缓存
        if req.cache_ttl > 0:
            hit = self.cache.get(self.primary.name, req.key)
            if hit is not None:
                return Result(ok=True, data=hit, source_type=self.primary.name)
        # ② 配额触顶 → 降级
        if self.quota.near_limit(self.primary.name):
            return self._fallback(req, "quota_near_limit")
        # ③ 限流
        if not self.limiter.allow(self.primary.name):
            return self._fallback(req, "rate_limited")
        # ④ 熔断
        if self.breaker.is_open(self.primary.name):
            return self._fallback(req, "circuit_open")
        # ⑤ 主调
        try:
            r = self.primary.call(req)
        except Exception as e:  # 网络异常等
            self.breaker.record_failure(self.primary.name)
            return self._fallback(
                req,
                f"exception:{type(e).__name__}",
                cause={"type": type(e).__name__, "detail": str(e)[:1000]},
            )
        if r.ok:
            self.breaker.record_success(self.primary.name)
            self.quota.incr(self.primary.name)
            if req.cache_ttl > 0 and r.data:
                self.cache.set(self.primary.name, req.key, r.data, req.cache_ttl)
            return r
        # 主调业务失败（ok=False）→ 记失败并降级
        self.breaker.record_failure(self.primary.name)
        return self._fallback(
            req,
            f"primary_not_ok:{r.source_type}",
            cause=r.error,
        )

    def _fallback(
        self,
        req: Req,
        reason: str,
        cause: dict | None = None,
    ) -> Result:
        for fb in self.fallbacks:
            try:
                r = fb.call(req)
                r.degraded = True
                r.degraded_reason = reason
                r.error = cause or r.error or {"type": reason}
                return r
            except Exception:
                continue
        return Result(
            ok=False,
            data=None,
            source_type="unknown",
            degraded=True,
            error=cause or {"type": reason},
            degraded_reason=reason,
        )
