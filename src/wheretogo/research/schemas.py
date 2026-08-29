"""Deep Research 的可观测质量契约。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ResearchQuality(BaseModel):
    """反思节点使用的可解释充分性，而不是只看结果数量。"""

    model_config = ConfigDict(extra="forbid")

    activity_count: int = 0
    distinct_entity_count: int = 0
    evidence_count: int = 0
    source_count: int = 0
    official_count: int = 0
    query_count: int = 0
    round_count: int = 0
    coverage: float = 0.0
    criterion_coverage: float = 0.0
    semantic_match_count: int = 0
    semantic_evaluated: bool = False
    marginal_gain: float = 0.0
    termination: str = "not_run"
    sufficient: bool = False
    gaps: list[str] = Field(default_factory=list)


class ResearchStop(BaseModel):
    """每次停止都必须说明为什么，便于 UI、评测和线上诊断。"""

    model_config = ConfigDict(extra="forbid")

    reason: Literal[
        "quality_sufficient",
        "personalized_rerank",
        "max_loops",
        "budget_exhausted",
        "no_better_alternatives",
        "continue_for_gap",
        "semantic_judge_unavailable",
        "semantic_judge_partial",
        "provider_unavailable",
    ]
    detail: str
