"""DD-17 实时深度研究（联网深搜 + 证据护栏 + 实时入库）。

入口 `deep_research()`：每次需求强制全量实时深搜，有界迭代，流式进度，实时入库，DD-03 护栏不动。
"""
from __future__ import annotations

from .brief import build_brief, split_subtopics
from .service import DeepResearchResult, ProgressEvent, deep_research, needs_deep_research

__all__ = [
    "deep_research", "needs_deep_research", "DeepResearchResult", "ProgressEvent",
    "build_brief", "split_subtopics",
]
