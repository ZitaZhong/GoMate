"""v4 动态前置条件解析（技术方案 v4 §4.2 / §7）。

事实不完整 != 任务不可执行：市内目标缺出发地必须先执行并给非阻塞提示；
跨城交通比较缺出发地才阻塞提问。
"""
from __future__ import annotations

from wheretogo.agent.decision import build_runtime_decision
from wheretogo.agent.prerequisites import (
    CAPABILITY_SPECS,
    ToolInputSpec,
    derive_known_facts,
    resolve_prerequisites,
)
from wheretogo.agent.status import TurnStatus
from wheretogo.copilot.turn_schema import TurnDecision


def _goals(*objectives: str) -> list[dict]:
    return [
        {"id": f"g{i}", "objective": obj, "required": True}
        for i, obj in enumerate(objectives)
    ]


# ---------- §4.2 场景表 ----------

def test_in_city_route_without_origin_is_executable_with_soft_hint():
    """上海市内四个已指定地点排一天路线：缺出发地 → 先规划，非阻塞提示。"""
    facts = derive_known_facts({"target_city_name": "上海"})
    resolution = resolve_prerequisites(
        _goals("把上海博物馆、世博馆、外滩和新天地安排成一天内顺路的路线"),
        [{"type": "research", "reason": "需要核实开放时间、位置和建议时长"}],
        facts,
    )
    assert len(resolution.executable_actions) == 1
    assert resolution.executable_actions[0]["type"] == "research"
    assert resolution.blocking_missing == []
    assert any(
        item["fact"] == "start_location" for item in resolution.non_blocking_missing
    )


def test_intercity_transport_without_origin_blocks():
    """比较杭州到上海的高铁和航班：缺出发城市 → 阻塞提问。"""
    facts = derive_known_facts({"target_city_name": "上海"})
    resolution = resolve_prerequisites(
        _goals("比较去上海坐高铁还是飞机"),
        [{"type": "transport_search", "reason": "跨城交通比较"}],
        facts,
    )
    assert resolution.executable_actions == []
    blocked = {item["fact"] for item in resolution.blocking_missing}
    assert "origin" in blocked
    question = next(
        item["question"] for item in resolution.blocking_missing
        if item["fact"] == "origin"
    )
    assert "出发" in question


def test_exhibition_recommendation_defaults_time_window():
    """推荐上海本周展览：缺精确时间 → 使用默认窗口继续研究，并记录假设。"""
    facts = derive_known_facts({"target_city_name": "上海"})  # 无 weekend_start
    resolution = resolve_prerequisites(
        _goals("推荐上海本周的展览"),
        [{"type": "research", "reason": "找展览"}],
        facts,
    )
    assert len(resolution.executable_actions) == 1
    assumptions = resolution.executable_actions[0]["assumptions"]
    assert any("默认" in text or "周末" in text for text in assumptions)


def test_recompose_without_candidates_blocks():
    """只重排但没有任何候选 → 阻塞（不能凭空编排）。"""
    facts = derive_known_facts({"target_city_name": "上海"}, latest_results={})
    resolution = resolve_prerequisites(
        _goals("重排现有行程"),
        [{"type": "compose_itinerary", "reason": "重排"}],
        facts,
    )
    assert resolution.executable_actions == []
    assert resolution.blocking_missing[0]["fact"] == "existing_candidates"


def test_recompose_with_candidates_executable():
    facts = derive_known_facts(
        {"target_city_name": "上海"},
        latest_results={"activities": [{"title": "上海博物馆"}]},
    )
    resolution = resolve_prerequisites(
        _goals("重排现有行程"),
        [{"type": "compose_itinerary", "reason": "重排"}],
        facts,
    )
    assert resolution.executable_actions[0]["type"] == "compose_itinerary"
    assert resolution.blocking_missing == []


# ---------- 工具契约结构 ----------

def test_tool_input_spec_requirement_levels():
    spec_map = {s.name: s for s in CAPABILITY_SPECS["research"]}
    assert spec_map["destination"].requirement == "hard"
    assert spec_map["start_location"].requirement == "soft"
    assert spec_map["time_window"].requirement == "defaultable"
    transport = {s.name: s for s in CAPABILITY_SPECS["transport_search"]}
    assert transport["origin"].requirement == "hard"
    assert transport["travel_date"].requirement == "defaultable"


def test_unknown_capability_neither_executes_nor_blocks():
    resolution = resolve_prerequisites(
        _goals("目标"),
        [{"type": "teleport", "reason": "不存在的能力"}],
        {"destination": "上海"},
    )
    assert resolution.executable_actions == []
    assert resolution.blocking_missing == []


def test_custom_tool_spec_injection():
    specs = {
        "book_hotel": [ToolInputSpec("travel_date", "hard", description="入住日期")],
    }
    resolution = resolve_prerequisites(
        _goals("预订指定日期酒店"),
        [{"type": "book_hotel", "reason": "预订"}],
        {},
        tool_specs=specs,
    )
    # 缺日期 → 阻塞提问，不得猜测并执行预订（§4.2 表）
    assert resolution.executable_actions == []
    assert resolution.blocking_missing[0]["fact"] == "travel_date"


def test_origin_only_constraints_research_local():
    """只有出发地（AI 主动推荐模式）：目的地默认当地 → research 可执行。"""
    facts = derive_known_facts({"origins": ["杭州"]})
    assert facts["destination"] == "杭州"
    resolution = resolve_prerequisites(
        _goals("这个周末去哪玩"),
        [{"type": "research", "reason": "主动调研"}],
        facts,
    )
    assert resolution.executable_actions


# ---------- 部分执行（§7.3）：可做的先做，阻塞部分转非阻塞提示 ----------

def test_partial_execution_research_runs_while_transport_blocked():
    interpreted = TurnDecision(
        primary_intent="provide_constraints",
        acts=["research_more"],
        goals=_goals("核实四个地点并生成市内路线", "比较跨城交通"),
        proposed_actions=[
            {"type": "research", "reason": "核实地点"},
            {"type": "transport_search", "reason": "比较交通"},
        ],
    )
    facts = derive_known_facts({"target_city_name": "上海"})
    resolution = resolve_prerequisites(
        interpreted.goals, interpreted.proposed_actions, facts
    )
    assert resolution.executable_actions  # research 可执行
    assert resolution.blocking_missing  # transport 缺 origin
    result = build_runtime_decision(interpreted, resolution)
    assert result.status == TurnStatus.RUNNING  # 不因 transport 阻塞整轮
    assert result.clarification is not None
    assert result.clarification.blocking is False  # 转为非阻塞提示
