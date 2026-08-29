"""AI 能力层：脱敏 `redact` + 结构化抽取（DD-04 §6.2 / §6.4）。

- `redact`：进 LLM 前强制——手机/证件/银行卡打码、门牌楼栋粗化、精确预算区间化。
- `extract_json` / `extract_fact`：LLM 抽取→JSON→Fact。**LLM 产物恒为 estimated**
  （DD-03：source_type=llm 永不得 confirmed）。无 key/失败 → 返回 None，调用方走确定性规则。
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

from ..enums import SourceType
from ..schemas.evidence import Evidence, Fact
from .llm import chat

_PHONE = re.compile(r"1[3-9]\d{9}")
_IDCARD = re.compile(r"\d{17}[\dXx]")
_BANKCARD = re.compile(r"\b\d{16,19}\b")
# 门牌/楼栋/单元/室 等精确地址后缀 → 粗化（对齐 DD-01 origin_area 商圈级粒度）
# 注意：中文数字逐字列举，不用范围（十/百 codepoint 小于零，会构成非法范围）
_ADDR_DETAIL = re.compile(r"[0-9零一二三四五六七八九十百千万]+(?:号楼|单元|楼层|号|栋|幢|室|层)")


def redact(payload: Any) -> Any:
    """递归脱敏：返回深拷贝，原对象不变。模型侧永不见原始 PII。"""
    return _redact(copy.deepcopy(payload))


def _redact(o: Any) -> Any:
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            if k in {"phone", "mobile", "id_number", "id_card", "contact"} and isinstance(v, str):
                out[k] = "***********"
            elif k in {"budget", "budget_amount"} and isinstance(v, (int, float)) and v > 0:
                out[k] = _bandify_budget(v)
            else:
                out[k] = _redact(v)
        return out
    if isinstance(o, list):
        return [_redact(v) for v in o]
    if isinstance(o, str):
        return _redact_str(o)
    return o


def _redact_str(s: str) -> str:
    s = _PHONE.sub("***********", s)
    s = _IDCARD.sub("******************", s)
    s = _BANKCARD.sub("********", s)
    s = _ADDR_DETAIL.sub("**", s)
    return s


def _bandify_budget(amount: float) -> str:
    """精确预算 → 区间（如 3500 → '3000-4000'）。"""
    step = 1000
    lo = int(amount // step) * step
    return f"{lo}-{lo + step}"


def _safe_json(raw: str) -> dict | None:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        # 去除 ```json ... ``` 围栏
        segs = [seg for seg in raw.split("```") if seg.strip().strip("`").strip()]
        raw = max(segs, key=len).strip().lstrip("json").strip() if segs else raw
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
                return obj if isinstance(obj, dict) else None
            except Exception:
                return None
        return None


def extract_json(
    task: str,
    instruction: str,
    text: str,
    byo_key: str | None = None,
    *,
    timeout: float = 60.0,
) -> dict | None:
    """LLM 抽取并解析为 dict；无 key/失败 → None。"""
    messages = [
        {"role": "system", "content": f"{instruction}\n严格只输出一个 JSON 对象，不要解释、不要 Markdown。"},
        {"role": "user", "content": text},
    ]
    raw = (
        chat(task, messages, byo_key=byo_key)
        if timeout == 60.0
        else chat(task, messages, byo_key=byo_key, timeout=timeout)
    )
    if not raw:
        return None
    return _safe_json(raw)


def extract_fact(
    task: str,
    text: str,
    fields: dict[str, str],
    source_type: SourceType = SourceType.llm,
    byo_key: str | None = None,
) -> dict[str, Fact] | None:
    """抽取命名字段 → {field: Fact}。

    fields: {field_name: description}。每个 Fact 带 estimated evidence（LLM 不得 confirmed）。
    无 key/失败 → None（调用方走确定性规则）。
    """
    schema_desc = "\n".join(f"- {k}: {v}" for k, v in fields.items())
    instruction = f"从文本抽取以下字段，输出 JSON 对象；缺失字段给 null：\n{schema_desc}"
    parsed = extract_json(task, instruction, text, byo_key=byo_key)
    if not isinstance(parsed, dict):
        return None
    return {
        k: Fact(value=v, evidence=Evidence.estimated(source_type=source_type, note=f"extract:{task}"))
        for k, v in parsed.items()
    }
