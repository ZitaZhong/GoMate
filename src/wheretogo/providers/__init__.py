"""DD-04 外部 Provider 抽象与 AI 能力层（v2：API + 确定性兜底）。

统一入口：`providers.call(name, op, params)` → Result。AI 能力：`chat/redact/extract_fact`。
所有领域模块经本层访问外部世界，不得裸调外部 API。
"""
from __future__ import annotations

from .ai import extract_fact, extract_json, redact
from .base import Provider, Req, Result, TTL_BY_OP
from .llm import LLM_ROUTES, chat, get_llm_config
from .registry import call, get_resilient, has_key, reset_registry_for_tests
from .resilient import ResilientProvider

__all__ = [
    # 抽象
    "Req", "Result", "Provider", "TTL_BY_OP",
    # 韧性入口
    "ResilientProvider", "get_resilient", "call", "has_key", "reset_registry_for_tests",
    # AI 能力
    "chat", "redact", "extract_fact", "extract_json",
    "LLM_ROUTES", "get_llm_config",
]
