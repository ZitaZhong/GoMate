"""动态前置条件解析（v4 §7）。

删除"全局是否能规划"的判断：前置条件由当前目标、工具输入契约和已知事实动态决定。
这里枚举的是工具参数（能力集合封闭），不是用户意图或语言空间（语义保持开放）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Requirement = Literal["hard", "soft", "defaultable"]


@dataclass(frozen=True)
class ToolInputSpec:
    """单个工具输入的契约（v4 §7.2）。"""

    name: str
    requirement: Requirement
    default_strategy: str | None = None
    description: str = ""


#: 能力 → 输入契约。新增开放体验需求不需要修改该表；只有新增工具能力时才声明。
CAPABILITY_SPECS: dict[str, list[ToolInputSpec]] = {
    # 目的地城市内查找/核实活动与地点；出发地只是首段交通优化的软输入。
    "research": [
        ToolInputSpec("destination", "hard", description="研究的城市或区域"),
        ToolInputSpec(
            "time_window", "defaultable", default_strategy="current_planning_window",
            description="研究/出行的时间窗口",
        ),
        ToolInputSpec(
            "start_location", "soft",
            description="市内路线第一段交通的起点（酒店/车站）",
        ),
    ],
    # 跨城大交通比较必须知道从哪里出发。
    "transport_search": [
        ToolInputSpec("origin", "hard", description="跨城交通的出发城市或区域"),
        ToolInputSpec("destination", "hard", description="跨城交通的目的城市"),
        ToolInputSpec(
            "travel_date", "defaultable", default_strategy="current_planning_window",
            description="出行日期",
        ),
    ],
    # 只用已有候选重排，不做外部搜索。
    "compose_itinerary": [
        ToolInputSpec("existing_candidates", "hard", description="当前已有的研究候选"),
    ],
    "answer": [],
    "booking": [],
    "replan": [],
}

#: 阻塞事实 → 用户可见问题（wording 固定在运行时，不依赖模型措辞）
BLOCKING_QUESTIONS: dict[str, str] = {
    "origin": "你从哪个城市出发？",
    "destination": "想去哪个城市或区域？",
    "existing_candidates": "当前还没有可用的候选结果，要先按你的需求做一轮研究吗？",
}

#: 非阻塞事实 → 提示语（不阻断执行，只说明补充后能优化什么）
NON_BLOCKING_HINTS: dict[str, str] = {
    "start_location": "如果你告诉我酒店或抵达车站的位置，我还能优化第一段交通。",
    "origin": "如果你告诉我出发地，我还能补充跨城交通建议。",
}

#: 默认策略 → 假设文本（写入 Turn/Run，不静默应用）
DEFAULT_ASSUMPTIONS: dict[str, str] = {
    "current_planning_window": "按当前规划窗口（默认本周末）安排时间",
    "full_day": "按全天时间窗口安排",
    "first_stop_start": "市内路线从第一个地点开始计算",
}


@dataclass
class PrerequisiteResolution:
    """v4 §7.1 的解析结果：可执行部分立即执行，阻塞部分转为澄清。"""

    executable_actions: list[dict] = field(default_factory=list)
    blocking_missing: list[dict] = field(default_factory=list)
    non_blocking_missing: list[dict] = field(default_factory=list)


def derive_known_facts(
    constraints: dict | None,
    latest_results: dict | None = None,
) -> dict:
    """从当前约束与研究工作区推导事实表；只做存在性判断，不产生新事实。"""
    c = dict(constraints or {})
    results = dict(latest_results or {})
    facts: dict = {}
    origins = [o for o in (c.get("origins") or []) if str(o).strip()]
    if origins:
        facts["origin"] = origins
    # 目的地：显式目标城市优先；只有出发地时按"出发地当地"研究（AI 主动推荐）。
    destination = (
        c.get("target_city_name")
        or c.get("target_city_code")
        or (origins[0] if origins else None)
    )
    if destination:
        facts["destination"] = destination
    if c.get("weekend_start"):
        facts["time_window"] = {
            "start": c.get("weekend_start"),
            "end": c.get("weekend_end"),
        }
        facts["travel_date"] = c.get("weekend_start")
    if c.get("start_location"):
        facts["start_location"] = c.get("start_location")
    if results.get("activities") or results.get("research_context"):
        facts["existing_candidates"] = True
    return facts


def resolve_prerequisites(
    goals: list[dict] | None,
    proposed_actions: list[dict] | None,
    known_facts: dict | None,
    tool_specs: dict[str, list[ToolInputSpec]] | None = None,
) -> PrerequisiteResolution:
    """把动作提案按工具输入契约分成：可执行 / 阻塞缺失 / 非阻塞缺失。

    事实不完整 != 任务不可执行：hard 缺失才阻塞该动作；defaultable 记入假设；
    soft 缺失只生成非阻塞提示。同一事实对不同动作可能既是 hard 又是 soft。
    """
    specs = tool_specs or CAPABILITY_SPECS
    facts = dict(known_facts or {})
    goal_text = "；".join(
        str(g.get("objective") or "").strip()
        for g in (goals or [])
        if str(g.get("objective") or "").strip()
    )
    resolution = PrerequisiteResolution()
    seen_blocking: set[str] = set()
    seen_non_blocking: set[str] = set()
    for action in proposed_actions or []:
        action_type = str(action.get("type") or "").strip()
        inputs = specs.get(action_type)
        if inputs is None:
            continue  # 未注册能力：既不执行也不阻塞（封闭动作空间）
        missing_hard: list[ToolInputSpec] = []
        assumptions: list[str] = []
        soft_missing: list[ToolInputSpec] = []
        for spec in inputs:
            if facts.get(spec.name):
                continue
            if spec.requirement == "hard":
                missing_hard.append(spec)
            elif spec.requirement == "defaultable":
                assumptions.append(
                    DEFAULT_ASSUMPTIONS.get(
                        spec.default_strategy or "", f"使用默认{spec.description or spec.name}"
                    )
                )
            else:  # soft
                soft_missing.append(spec)
        if missing_hard:
            for spec in missing_hard:
                if spec.name in seen_blocking:
                    continue
                seen_blocking.add(spec.name)
                resolution.blocking_missing.append({
                    "fact": spec.name,
                    "reason": spec.description or action.get("reason") or "",
                    "required_by": [action_type],
                    "question": BLOCKING_QUESTIONS.get(spec.name, f"请补充：{spec.description or spec.name}"),
                })
            continue
        resolution.executable_actions.append({
            "type": action_type,
            "goal": goal_text or str(action.get("reason") or ""),
            "assumptions": assumptions,
        })
        for spec in soft_missing:
            if spec.name in seen_non_blocking:
                continue
            seen_non_blocking.add(spec.name)
            resolution.non_blocking_missing.append({
                "fact": spec.name,
                "reason": spec.description,
                "hint": NON_BLOCKING_HINTS.get(
                    spec.name, f"补充{spec.description or spec.name}后可以进一步优化结果。"
                ),
            })
    # 某个事实在可执行动作中只是 soft，但在被阻塞动作中是 hard：保留两种记录，
    # 由决策层判断——只要存在可执行动作，就不因该事实阻塞整个回合。
    return resolution
