"""Provider 注册表：按 Settings 装配 ResilientProvider（DD-04 §3/§5）。

- 有 key：primary=真 Provider + fallback=确定性兜底（主调失败时降级）。
- 无 key：primary 直接是确定性兜底（离线可测；cache_ttl 强制 0，不缓存确定性结果）。

共享单例 gear（cache/limiter/breaker/quota）跨 provider。
"""
from __future__ import annotations

from ..config import Settings, get_settings
from .amap import AmapFallback, AmapProvider
from .base import TTL_BY_OP, Req, Result
from .gear import Cache, CircuitBreaker, Limiter, Quota
from .llm import LLMFallback, LLMProvider
from .qweather import QWeatherFallback, QWeatherProvider
from .resilient import ResilientProvider
from .search import SearchFallback, SearchProvider
from .variflight import VariFlightFallback, VariFlightProvider

# provider 名 → (real, fallback)
_PROFILES = {
    "amap": (AmapProvider, AmapFallback),
    "qweather": (QWeatherProvider, QWeatherFallback),
    "variflight": (VariFlightProvider, VariFlightFallback),
    "search": (SearchProvider, SearchFallback),
    "web_search_deep": (SearchProvider, SearchFallback),  # 深搜编排由 research 层承担
    "llm": (LLMProvider, LLMFallback),
}

# 进程级共享单例
_cache = Cache()
_limiter = Limiter()
_breaker = CircuitBreaker()
_quota = Quota()
_instances: dict[str, ResilientProvider] = {}


def _key_for(name: str, s: Settings) -> str:
    return {
        "amap": s.amap_key,
        "qweather": s.qweather_key,
        "variflight": s.variflight_key,
        "search": s.search_api_key,
        "web_search_deep": s.search_api_key,
        "llm": s.llm_api_key,
    }.get(name, "")


def has_key(name: str) -> bool:
    return bool(_key_for(name, get_settings()))


def get_resilient(name: str) -> ResilientProvider:
    """装配并缓存（settings 不变则复用）。"""
    if name in _instances:
        return _instances[name]
    if name not in _PROFILES:
        raise KeyError(f"未知 provider: {name}")
    real_cls, fb_cls = _PROFILES[name]
    if has_key(name):
        rp = ResilientProvider(
            real_cls(), [fb_cls()],
            cache=_cache, limiter=_limiter, breaker=_breaker, quota=_quota,
        )
    else:
        # 无 key：primary 即确定性兜底；不缓存确定性结果（cache_ttl 由 call 强制 0）
        rp = ResilientProvider(
            fb_cls(), [],
            cache=_cache, limiter=_limiter, breaker=_breaker, quota=_quota,
        )
    _instances[name] = rp
    return rp


def call(
    name: str, op: str, params: dict, cache_ttl: int | None = None
) -> Result:
    """一键调用：name=provider，op/params 见各 Provider；cache_ttl 默认查 TTL_BY_OP。"""
    if cache_ttl is None:
        cache_ttl = 0 if not has_key(name) else TTL_BY_OP.get(op, 0)
    req = Req(op=op, params=params, cache_ttl=cache_ttl)
    return get_resilient(name).call(req)


def reset_registry_for_tests() -> None:
    """测试用：清空单例缓存（在改了 settings / 注入 mock 后重建）。"""
    _instances.clear()


__all__ = [
    "get_resilient", "call", "has_key", "reset_registry_for_tests",
    "ResilientProvider", "Req", "Result",
]
