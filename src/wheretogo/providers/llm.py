"""LLM 路由 + 模型中立客户端（DD-04 §6）。

OpenAI 兼容端点；BYO key；分层路由（task→model）。LLM 为主路径；无 key → chat 返回 None，
调用方仅作显式标注的确定性降级（degraded/estimated），禁止静默兜底。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from ..config import get_settings
from ._net import post_json
from .base import Req, Result

logger = logging.getLogger("uvicorn.error")

# task → 模型（含 v2 增补路由，DD-04 §13）
LLM_ROUTES: dict[str, str] = {
    "constraint_parse": "qwen-plus",
    "activity_extract": "qwen-turbo",
    "search_entry": "qwen-turbo",
    "booking_ocr": "qwen-vl-ocr",
    "research_reason": "qwen-max",
    # v2
    "intent_classify": "qwen-turbo",
    "qa_answer": "qwen-plus",
    "research_brief": "qwen-plus",
    "research_supervisor": "qwen-plus",
    "research_extract": "qwen-turbo",
    "memory_extract": "qwen-turbo",
    "default": "qwen-plus",
}


def get_llm_config(task: str, byo_key: str | None = None) -> dict:
    s = get_settings()
    key = byo_key or s.llm_api_key
    # llm_use_routes=False（默认）：通用 OpenAI 兼容端点，全部任务用 llm_model_default
    # llm_use_routes=True：面向 Qwen/DashScope 的分层路由（LLM_ROUTES）
    model = LLM_ROUTES.get(task, s.llm_model_default) if s.llm_use_routes else s.llm_model_default
    return {
        "base_url": s.llm_base_url.rstrip("/"),
        "model": model,
        "key": key,
        "available": bool(key),
    }


class LLMProvider:
    """LLM 作为 Provider（op=chat）。无 key → ok=False（触发确定性兜底）。"""

    name = "llm"

    def call(self, req: Req) -> Result:
        cfg = get_llm_config(req.params.get("task", "default"), req.params.get("byo_key"))
        if not cfg["available"]:
            return Result(ok=False, data=None, source_type="llm")
        from .ai import redact  # 延迟导入避免与 ai.py 循环依赖（P1-10：入模型前强制脱敏）
        try:
            data: dict[str, Any] = post_json(
                f"{cfg['base_url']}/chat/completions",
                headers={"Authorization": f"Bearer {cfg['key']}"},
                body={"model": cfg["model"], "messages": redact(req.params.get("messages", []))},
            )
            text = data["choices"][0]["message"]["content"]
            return Result(ok=True, data={"text": text, "model": cfg["model"]}, source_type="llm")
        except Exception:
            return Result(ok=False, data=None, source_type="llm")


class LLMFallback:
    """LLM 无通用确定性兜底；返回 unknown，调用方各自走规则路径。"""

    name = "llm"

    def call(self, req: Req) -> Result:
        return Result(ok=False, data=None, source_type="unknown", degraded=True)


def chat(task: str, messages: list[dict], byo_key: str | None = None,
         timeout: float = 60.0) -> str | None:
    """便捷调用：返回文本或 None（无 key/失败）。timeout 默认 60s（大正文抽取可调高）。"""
    cfg = get_llm_config(task, byo_key)
    if not cfg["available"]:
        logger.info("llm_call task=%s status=unavailable", task)
        return None
    from .ai import redact  # 延迟导入避免循环（P1-10：手机/证件/银行卡/精确地址/预算入模型前脱敏）
    started = time.monotonic()
    try:
        data = post_json(
            f"{cfg['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {cfg['key']}"},
            body={"model": cfg["model"], "messages": redact(messages)},
            timeout=timeout,
        )
        content = data["choices"][0]["message"]["content"]
        logger.info(
            "llm_call task=%s model=%s status=ok elapsed_ms=%d",
            task,
            cfg["model"],
            int((time.monotonic() - started) * 1000),
        )
        return content
    except Exception as exc:
        logger.warning(
            "llm_call task=%s model=%s status=failed error=%s elapsed_ms=%d",
            task,
            cfg["model"],
            type(exc).__name__,
            int((time.monotonic() - started) * 1000),
        )
        return None
