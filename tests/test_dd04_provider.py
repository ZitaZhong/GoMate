"""DD-04 外部 Provider 抽象与 AI 能力层验收（对应 DD-04 §10 DoD）。

纯离线测试：用 fake Provider 驱动五件套，验证缓存/限流/熔断/配额/降级/脱敏/抽取。
不依赖真实外部 key 与网络。
"""
from __future__ import annotations

import httpx

from wheretogo.enums import SourceType, VerificationStatus
from wheretogo.providers import ai, extract_fact, redact
from wheretogo.providers.base import Req, Result
from wheretogo.providers.gear import Cache, CircuitBreaker, Limiter, Quota
from wheretogo.providers.resilient import ResilientProvider
from wheretogo.providers.search import SearchProvider


class _FakePrimary:
    """可计数的假主调：默认成功，可配置抛异常/返回 ok=False。"""

    name = "amap"

    def __init__(self, *, fail: bool = False, raise_: bool = False) -> None:
        self.fail = fail
        self.raise_ = raise_
        self.calls = 0

    def call(self, req: Req) -> Result:
        self.calls += 1
        if self.raise_:
            raise RuntimeError("boom")
        if self.fail:
            return Result(ok=False, data=None, source_type="amap")
        return Result(ok=True, data={"distance_m": 1234, "duration_min": 20}, source_type="amap")


class _FakeFallback:
    name = "amap"

    def __init__(self) -> None:
        self.calls = 0

    def call(self, req: Req) -> Result:
        self.calls += 1
        return Result(ok=True, data={"distance_m": 9999, "estimate": True}, source_type="amap")


def _req(op: str = "route", cache_ttl: int = 0) -> Req:
    return Req(op=op, params={"origin": [1, 2], "destination": [3, 4]}, cache_ttl=cache_ttl)


# —— DoD 1：降级链 ——
def test_fallback_on_primary_exception_marks_degraded():
    primary = _FakePrimary(raise_=True)
    fb = _FakeFallback()
    rp = ResilientProvider(primary, [fb])
    r = rp.call(_req())
    assert r.ok and r.degraded is True
    assert r.data["estimate"] is True
    assert fb.calls == 1


def test_fallback_on_primary_not_ok():
    primary = _FakePrimary(fail=True)
    fb = _FakeFallback()
    rp = ResilientProvider(primary, [fb])
    r = rp.call(_req())
    assert r.ok and r.degraded is True


def test_search_provider_preserves_non_retryable_http_failure(monkeypatch):
    provider = SearchProvider()
    provider._provider = "tavily"
    provider._key = "test-key"

    def fail_with_usage_limit(*_args, **_kwargs):
        request = httpx.Request("POST", "https://api.tavily.com/search")
        response = httpx.Response(432, request=request, text="usage limit")
        raise httpx.HTTPStatusError(
            "usage limit",
            request=request,
            response=response,
        )

    monkeypatch.setattr(provider, "_dispatch", fail_with_usage_limit)
    result = provider.call(Req(op="web_search", params={"query": "Hangzhou"}))

    assert result.ok is False
    assert result.error == {
        "type": "http_error",
        "provider": "tavily",
        "status_code": 432,
        "detail": "usage limit",
        "retryable": False,
    }


def test_fallback_keeps_primary_failure_diagnostics():
    class FailedPrimary:
        name = "search"

        def call(self, _req):
            return Result(
                ok=False,
                data=None,
                source_type="search",
                error={
                    "provider": "tavily",
                    "status_code": 432,
                    "retryable": False,
                },
            )

    result = ResilientProvider(FailedPrimary(), [_FakeFallback()]).call(_req())

    assert result.ok is True
    assert result.degraded is True
    assert result.error["status_code"] == 432
    assert result.degraded_reason.startswith("primary_not_ok")


def test_fallback_exhausts_to_unknown():
    primary = _FakePrimary(raise_=True)
    rp = ResilientProvider(primary, [])  # 无 fallback
    r = rp.call(_req())
    assert r.ok is False and r.source_type == "unknown" and r.degraded is True


# —— DoD 2：缓存命中（二次不调主调）——
def test_cache_hit_skips_primary():
    primary = _FakePrimary()
    rp = ResilientProvider(primary, [], cache=Cache(redis=False))  # 内存缓存（隔离 Redis）
    rp.call(_req(cache_ttl=60))  # 首次：主调 + 写缓存
    r2 = rp.call(_req(cache_ttl=60))  # 二次：命中缓存
    assert primary.calls == 1
    assert r2.ok and r2.data["distance_m"] == 1234


# —— DoD 3：限流触发降级 ——
def test_rate_limit_triggers_degradation():
    primary = _FakePrimary()
    limiter = Limiter(default_rate=0.0, default_capacity=2)  # 极小桶：2 个令牌后耗尽
    rp = ResilientProvider(primary, [], limiter=limiter)
    rp.call(_req())  # 用掉 1
    rp.call(_req())  # 用掉 2
    r3 = rp.call(_req())  # 令牌耗尽 → 无 fallback → unknown degraded
    assert r3.degraded is True
    assert primary.calls == 2  # 第 3 次没打到主调


# —— 熔断：连续失败后 open，停止打主调 ——
def test_circuit_opens_after_failures():
    primary = _FakePrimary(raise_=True)
    breaker = CircuitBreaker(fail_threshold=3, recovery_successes=2)
    rp = ResilientProvider(primary, [], breaker=breaker)
    for _ in range(3):
        rp.call(_req())
    assert breaker.is_open("amap") or breaker.state("amap") in ("open", "half_open")
    calls_before = primary.calls
    rp.call(_req())  # 熔断中：不打主调
    assert primary.calls == calls_before


# —— 配额触顶 → 降级 ——
def test_quota_near_limit_degrades():
    primary = _FakePrimary()
    quota = Quota()
    quota.set_limit("amap", 100)
    for _ in range(91):
        quota.incr("amap")  # 91 >= 90% of 100
    assert quota.near_limit("amap")
    rp = ResilientProvider(primary, [], quota=quota)
    r = rp.call(_req())
    assert r.degraded is True
    assert primary.calls == 0  # 没打主调


# —— DoD 4：脱敏 ——
def test_redact_masks_pii():
    out = redact({
        "phone": "13912345678", "id_number": "310101199001011234",
        "bank": "6222020200112345678", "addr": "天钥桥路333号A栋12层",
        "budget": 3500, "deep": {"contact": "13800001111", "note": "x"},
    })
    for s in (str(out["phone"]), str(out["id_number"]), str(out["bank"]),
              str(out["deep"]["contact"])):
        assert "13912345678" not in s and "310101199001011234" not in s
        assert "6222020200112345678" not in s and "13800001111" not in s
    assert out["budget"] == "3000-4000"  # 区间化
    assert "333号" not in out["addr"]  # 门牌粗化


def test_redact_does_not_mutate_original():
    original = {"phone": "13912345678"}
    redact(original)
    assert original["phone"] == "13912345678"  # 原对象不变


# —— DoD 5/6：抽取产 Fact，llm 来源恒 estimated ——
def test_extract_fact_produces_estimated_facts(monkeypatch):
    monkeypatch.setattr(ai, "chat", lambda task, messages, byo_key=None: '{"title": "古埃及展", "price": null}')
    facts = extract_fact("activity_extract", "原文…", {"title": "活动名", "price": "价格"})
    assert facts is not None
    assert facts["title"].value == "古埃及展"
    assert facts["title"].evidence.verification_status == VerificationStatus.estimated
    assert facts["title"].evidence.source_type == SourceType.llm  # LLM 不得 confirmed（Guard 规则）


def test_extract_fact_none_without_llm():
    # 无 key（默认）→ chat 返回 None → extract_fact 返回 None
    assert extract_fact("activity_extract", "原文", {"title": "名"}) is None
