"""Build an open-ended research brief.

The brief preserves the user's goal and acceptance criteria as free text.
Executable tools are finite; travel topics are not.
"""
from __future__ import annotations

from .semantics import plan_research_tasks


def build_brief(city_code: str, weekend, categories: list[str] | None,
                nl_query: str | None, interests: list[str] | None = None,
                city_name: str | None = None,
                research_goal: str | None = None,
                acceptance_criteria: list[str] | None = None,
                research_subgoals: list[dict] | None = None,
                scope: str = "cross_city") -> dict:
    """Create a north-star brief without mapping the goal to a topic enum.

    scope="local"（DD-18 市内模式）：研究目标改写为「{城市} 市内 本周末 {theme}」，
    任务规划与回退查询均围绕城内当周主题活动展开（而非跨城周末游）。
    """
    legacy_requirements = [
        str(value).strip()
        for value in (categories or interests or [])
        if str(value).strip()
    ]
    goal = (research_goal or nl_query or "").strip()
    if scope == "local":
        # 市内模式：目标前缀城市，聚焦「城内 本周末 {theme}」（DD-18 §7）
        local_theme = goal or "；".join(legacy_requirements) or "热门活动推荐"
        goal = f"{city_name or '本地'} 市内 本周末 {local_theme}".strip()
    return {
        "city_code": city_code,
        "city_name": city_name or "",
        "weekend": {"start": _iso(weekend, "start"), "end": _iso(weekend, "end")},
        # Kept only so old checkpoints/cache payloads remain readable.
        "categories": legacy_requirements,
        "nl_query": (nl_query or "").strip(),
        "research_goal": goal or "；".join(legacy_requirements),
        "acceptance_criteria": list(dict.fromkeys(
            [
                str(value).strip()
                for value in (acceptance_criteria or legacy_requirements)
                if str(value).strip()
            ]
        )),
        "research_subgoals": list(research_subgoals or []),
        "scope": scope,
    }


def split_subtopics(brief: dict) -> list[str]:
    """Compatibility view for callers that only understand query strings."""
    return [task["query"] for task in plan_research_tasks(brief)]


def _iso(weekend, key):
    v = getattr(weekend, key, None) if weekend else None
    return v.isoformat() if hasattr(v, "isoformat") else v
