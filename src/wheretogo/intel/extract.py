"""LLM 抽取（DD-06 §5.4）：正文 → ActivityDraft 列表（带 evidence_quote 回锚）。

v0.1 不强依赖 PydanticAI：用 DD-04 `chat`（activity_extract 路由）+ JSON 指令产出草稿；
`quotes_grounded_in()` 在入库前逐字校验（防幻觉第一道工程护栏）。
无 key → 返回 []（调用方走官方源/审核兜底）。
"""
from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field, ValidationError

from ..providers.llm import chat

_EXTRACT_MAX_CHARS = 8000  # 截断正文（控 LLM 延迟；开放域正文多含导航噪声，8K 足够覆盖活动段）


class FieldQuote(BaseModel):
    """每个对外事实字段的原文引用，用于回锚校验。"""

    value: str | None = None
    evidence_quote: str | None = Field(None, description="支撑该字段的原文片段，必须逐字来自正文")


class ActivityDraft(BaseModel):
    title: str
    venue: str | None = None
    start_text: FieldQuote
    end_text: FieldQuote | None = None
    price: FieldQuote
    booking: FieldQuote | None = None
    category: str | None = None
    city_code: str | None = None
    source_url: str | None = None

    def quotes_grounded_in(self, clean_md: str) -> bool:
        """回锚校验：所有非空 evidence_quote 必须逐字出现在清洗正文里。"""
        norm = "".join(clean_md.split())
        for fq in (self.start_text, self.end_text, self.price, self.booking):
            if fq and fq.evidence_quote:
                if "".join(fq.evidence_quote.split()) not in norm:
                    return False
        return True

    def embed_text(self) -> str:
        return " ".join(
            filter(None, [self.title, self.venue or "", self.category or "", self.price.value or ""])
        )


_SYSTEM = (
    "你是活动信息抽取器。只从给定正文抽取【当周活动】，输出一个 JSON 数组。"
    "严格规则：①每个时间/价格/购票字段必须附 evidence_quote，逐字来自正文；"
    "②正文没有的信息一律留空，禁止臆造或推断价格/余票/营业时间；"
    "③字段结构：{title, venue, start_text:{value,evidence_quote}, "
    "end_text:{value,evidence_quote}, price:{value,evidence_quote}, "
    "booking:{value,evidence_quote}, category, city_code, source_url}；"
    "④只输出 JSON 数组，不要解释。"
)


def _parse_drafts(raw: str) -> list[ActivityDraft]:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        segs = [seg for seg in raw.split("```") if seg.strip().strip("`").strip()]
        raw = max(segs, key=len).strip().lstrip("json").strip() if segs else raw
    try:
        obj = json.loads(raw)
    except Exception:
        m = re.search(r"\[.*\]", raw, re.S) or re.search(r"\{.*\}", raw, re.S)
        if not m:
            return []
        try:
            obj = json.loads(m.group(0))
        except Exception:
            return []
    if isinstance(obj, dict):
        obj = [obj]
    drafts: list[ActivityDraft] = []
    for item in obj:
        try:
            drafts.append(ActivityDraft.model_validate(item))
        except ValidationError:
            continue
    return drafts


def extract_activities(
    clean_md: str, city_code: str, source_url: str, weekend=None
) -> list[ActivityDraft]:
    """LLM 抽取活动草稿列表。无 key/失败 → []。"""
    if not clean_md:
        return []
    wk_start = getattr(weekend, "start", None)
    wk_end = getattr(weekend, "end", None)
    window = (
        f"{wk_start.isoformat() if hasattr(wk_start, 'isoformat') else wk_start or '未指定'}"
        f" 至 {wk_end.isoformat() if hasattr(wk_end, 'isoformat') else wk_end or '未指定'}"
    )
    body = (
        f"城市代码={city_code}\n来源URL={source_url}\n"
        f"目标出行时间窗={window}（活动区间与该窗口有重叠即可，长展不能因开展较早而漏掉）"
        f"\n\n正文:\n{clean_md[:_EXTRACT_MAX_CHARS]}"
    )
    raw = chat("activity_extract", [{"role": "system", "content": _SYSTEM},
                                    {"role": "user", "content": body}], timeout=180)
    if not raw:
        return []
    drafts = _parse_drafts(raw)
    for d in drafts:
        d.city_code = d.city_code or city_code
        d.source_url = d.source_url or source_url
    return drafts
