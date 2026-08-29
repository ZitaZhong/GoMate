"""RRF 倒数排名融合（DD-05 §5.3）。

只依赖排名、无需分数归一化，业界最常用、最鲁棒。
"""
from __future__ import annotations

from collections import defaultdict


def rrf(rank_lists: list[list[int]], k: int = 60) -> list[int]:
    scores: dict[int, float] = defaultdict(float)
    for ranks in rank_lists:
        for pos, doc_id in enumerate(ranks):
            scores[doc_id] += 1.0 / (k + pos)
    return sorted(scores, key=lambda d: scores[d], reverse=True)
