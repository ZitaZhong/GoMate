"""重排 query / 文本构造（DD-05 §6、§7）。

- rerank_query：把用户约束 + 偏好拼成 query，实现个性化排序（复用 DD-01 §8.1 的构造）。
- rerank_text：活动侧待排文本。
- rerank_query_dining：餐饮重排强调“动线契合（距离）+ 偏好 + 忌讳”。
"""
from __future__ import annotations

from ..schemas.constraints import build_rerank_query


def rerank_query(constraints: dict) -> str:
    return build_rerank_query(constraints)


def rerank_text(row) -> str:
    """活动行 -> 重排文本（title venue category price_text）。"""
    parts = [
        getattr(row, "title", "") or "",
        getattr(row, "venue", "") or "",
        getattr(row, "category", "") or "",
        getattr(row, "price_text", "") or "",
    ]
    return " ".join(p for p in parts if p).strip()


def rerank_query_dining(constraints: dict, meal_slot: str) -> str:
    cuisines = "、".join(constraints.get("cuisines") or constraints.get("interests") or []) or "不限"
    dietary = "、".join(constraints.get("dietary") or []) or "无"
    return f"距离近 {cuisines} 排除{dietary} {meal_slot}".strip()
