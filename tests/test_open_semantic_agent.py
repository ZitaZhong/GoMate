"""Open-world agent semantics: unseen concepts must not require code changes."""
from __future__ import annotations

from types import SimpleNamespace

from wheretogo.copilot.interpreter import interpret_turn
from wheretogo.copilot.respond import compose_research_response
from wheretogo.orchestration.nodes import _assess_research_quality
from wheretogo.research.semantics import (
    evaluate_candidates,
    extract_open_candidates,
    plan_research_tasks,
)


def test_turn_interpreter_keeps_unseen_requirements_as_open_text(monkeypatch):
    phrase = "想找废弃矿坑改造的摄影空间，不要商业化打卡点"
    monkeypatch.setattr(
        "wheretogo.copilot.interpreter.extract_json",
        lambda *_args, **_kwargs: {
            "primary_intent": "refine_field",
            "acts": ["update_constraints", "research_more"],
            "constraints": {
                "experience_requirements": [
                    "废弃矿坑改造的摄影空间",
                    "排除商业化打卡点",
                ]
            },
            "constraint_operations": [],
            "research_goal": phrase,
            "acceptance_criteria": [
                "是由废弃矿坑改造的具体地点",
                "有适合摄影的来源证据",
                "不是商业化打卡点",
            ],
            "confidence": 0.94,
        },
    )

    decision = interpret_turn(
        phrase,
        fallback_intent="provide_constraints",
        memory_ctx={"origins": ["上海"]},
        conversation=[],
        use_llm=True,
    )

    assert decision.constraints_patch["experience_requirements"] == [
        "废弃矿坑改造的摄影空间",
        "排除商业化打卡点",
    ]
    assert "interests" not in decision.constraints_patch
    assert decision.research_goal == phrase
    assert decision.acts == ["update_constraints", "research_more"]


def test_turn_interpreter_can_add_a_structured_subgoal(monkeypatch):
    museum = {
        "id": "museum",
        "objective": "看博物馆",
        "acceptance_criteria": ["找到有来源支持的可到访博物馆"],
        "required": True,
    }
    concert = {
        "id": "concert",
        "objective": "看演唱会",
        "acceptance_criteria": ["找到目标周末有来源支持的演唱会"],
        "required": True,
    }
    monkeypatch.setattr(
        "wheretogo.copilot.interpreter.extract_json",
        lambda *_args, **_kwargs: {
            "primary_intent": "refine_field",
            "acts": ["update_constraints", "research_more"],
            "constraints": {
                "experience_requirements": ["看博物馆", "看演唱会"],
                "research_subgoals": [museum, concert],
            },
            "constraint_operations": [
                {
                    "op": "add",
                    "field": "research_subgoals",
                    "value": concert,
                }
            ],
            "research_goal": "既看博物馆，也看演唱会",
            "acceptance_criteria": [
                "找到有来源支持的可到访博物馆",
                "找到目标周末有来源支持的演唱会",
            ],
            "confidence": 0.95,
        },
    )

    decision = interpret_turn(
        "我还想看演唱会",
        fallback_intent="refine_field",
        memory_ctx={
            "experience_requirements": ["看博物馆"],
            "research_subgoals": [museum],
        },
        conversation=[
            {"role": "user", "content": "下周末从上海去杭州看博物馆"},
            {"role": "assistant", "content": "已找到杭州博物馆等候选。"},
        ],
        latest_results={"activities": [{"title": "杭州博物馆"}]},
        use_llm=True,
    )

    assert decision.constraints_patch["experience_requirements"] == [
        "看博物馆",
        "看演唱会",
    ]
    assert decision.constraints_patch["research_subgoals"] == [museum, concert]
    assert "research_more" in decision.acts


def test_operation_scalar_is_renormalized_for_list_constraint(monkeypatch):
    requirement = "白天看博物馆，晚上看演唱会，结合在一份行程里"
    monkeypatch.setattr(
        "wheretogo.copilot.interpreter.extract_json",
        lambda *_args, **_kwargs: {
            "primary_intent": "provide_constraints",
            "acts": ["update_constraints", "research_more"],
            "constraints": {"experience_requirements": [requirement]},
            "constraint_operations": [
                {
                    "op": "set",
                    "field": "experience_requirements",
                    "value": requirement,
                }
            ],
            "research_goal": requirement,
            "acceptance_criteria": [requirement],
            "confidence": 0.95,
        },
    )

    decision = interpret_turn(
        "下周末从上海去杭州，白天看博物馆，晚上想看演唱会，请结合在一份行程里",
        fallback_intent="provide_constraints",
        memory_ctx={},
        conversation=[],
        use_llm=True,
    )

    assert decision.constraints_patch["experience_requirements"] == [requirement]


def test_research_planner_chooses_tools_for_arbitrary_goal(monkeypatch):
    monkeypatch.setattr(
        "wheretogo.research.semantics.extract_json",
        lambda *_args, **_kwargs: {
            "tasks": [
                {
                    "query": "杭州 废弃矿坑 改造 摄影",
                    "tool": "web_search",
                    "purpose": "find source-backed transformed sites",
                },
                {
                    "query": "废弃矿坑 摄影空间",
                    "tool": "map_places",
                    "purpose": "find concrete POIs",
                },
            ]
        },
    )
    tasks = plan_research_tasks({
        "city_name": "杭州",
        "research_goal": "废弃矿坑改造的摄影空间",
        "acceptance_criteria": ["排除商业化打卡点"],
        "weekend": {},
    })
    assert [task["tool"] for task in tasks] == ["web_search", "map_places"]
    assert all("category" not in task for task in tasks)


def test_open_candidate_extraction_is_not_event_only(monkeypatch):
    monkeypatch.setattr(
        "wheretogo.research.semantics.extract_json",
        lambda *_args, **_kwargs: {
            "candidates": [{
                "title": "某矿坑遗址公园",
                "kind": "工业遗址改造空间",
                "summary": "来源描述其由旧矿坑改造并保留岩壁景观",
                "venue": "杭州",
                "source_index": 0,
                "availability": {
                    "mode": "always",
                    "start": "None",
                    "end": "not-a-date",
                },
            }]
        },
    )
    candidates = extract_open_candidates(
        [{
            "title": "官方介绍",
            "url": "https://example.test/place",
            "content": "旧矿坑改造并保留岩壁景观，可在开放时段参观。",
        }],
        {
            "research_goal": "废弃矿坑改造的摄影空间",
            "acceptance_criteria": ["有具体地点和来源"],
        },
    )
    assert candidates[0]["candidate_kind"] == "工业遗址改造空间"
    assert candidates[0]["availability_mode"] == "always"
    assert candidates[0]["start_at"] is None
    assert candidates[0]["end_at"] is None
    assert candidates[0]["booking_url"] is None
    assert candidates[0]["evidence"]["source_url"] == "https://example.test/place"


def test_semantic_judge_filters_by_criteria_not_category_words(monkeypatch):
    monkeypatch.setattr(
        "wheretogo.research.semantics.extract_json",
        lambda *_args, **_kwargs: {
            "judgments": [
                {
                    "candidate_index": 0,
                    "match": True,
                    "score": 0.91,
                    "supported_criteria": ["由旧矿坑改造", "适合摄影"],
                    "contradicted_criteria": [],
                    "unknown_criteria": [],
                    "reason": "公开来源同时支持两项标准",
                },
                {
                    "candidate_index": 1,
                    "match": False,
                    "score": 0.1,
                    "supported_criteria": [],
                    "contradicted_criteria": ["不是矿坑改造"],
                    "unknown_criteria": [],
                    "reason": "普通商业影棚",
                },
            ],
            "criterion_coverage": 1.0,
            "gaps": [],
        },
    )
    ranked, evaluation = evaluate_candidates(
        [
            {"title": "旧矿坑空间", "description": "来源证据"},
            {"title": "商业影棚", "description": "来源证据"},
        ],
        research_goal="废弃矿坑改造的摄影空间",
        acceptance_criteria=["由旧矿坑改造", "适合摄影"],
        strict=True,
    )
    assert [item["title"] for item in ranked] == ["旧矿坑空间"]
    assert evaluation["matched_count"] == 1
    assert evaluation["criterion_coverage"] == 1.0


def test_quality_cannot_be_sufficient_without_semantic_acceptance():
    activities = [
        {"title": f"候选{i}", "evidence": {"source_url": f"https://e/{i}"}}
        for i in range(3)
    ]
    quality = _assess_research_quality(
        {
            "constraints": {
                "experience_requirements": ["完全未见过的开放需求"],
                "acceptance_criteria": ["必须有来源支持该需求"],
            },
            "research": {"status": "succeeded", "source_count": 8, "coverage": 1.0},
            "research_semantic_evaluation": {
                "evaluated": True,
                "matched_count": 0,
                "criterion_coverage": 0.0,
                "gaps": ["必须有来源支持该需求"],
            },
        },
        activities,
    )
    assert quality["sufficient"] is False
    assert "必须有来源支持该需求" in quality["gaps"]


def test_empty_candidates_do_not_call_semantic_judge(monkeypatch):
    called = False

    def unexpected_call(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("empty candidates must not call the model")

    monkeypatch.setattr(
        "wheretogo.research.semantics.extract_json",
        unexpected_call,
    )
    ranked, evaluation = evaluate_candidates(
        [],
        research_goal="寻找具体自然去处",
        acceptance_criteria=["候选本身是可到访地点"],
        strict=True,
    )
    assert ranked == []
    assert evaluation["matched_count"] == 0
    assert called is False


def test_semantic_outage_preserves_source_backed_open_candidates_only(monkeypatch):
    monkeypatch.setattr(
        "wheretogo.research.semantics.extract_json",
        lambda *_args, **_kwargs: None,
    )
    ranked, evaluation = evaluate_candidates(
        [
            {
                "title": "九溪十八涧",
                "candidate_type": "open_candidate",
                "evidence": {"source_url": "https://example.test/jiuxi"},
            },
            {
                "id": 7,
                "title": "无关历史活动",
                "evidence": {"source_url": "https://example.test/event"},
            },
            {
                "title": "模型凭空生成的地点",
                "candidate_type": "open_candidate",
                "evidence": {},
            },
        ],
        research_goal="杭州自然景点",
        acceptance_criteria=["候选本身是可到访的自然地点"],
        strict=True,
    )
    assert [item["title"] for item in ranked] == ["九溪十八涧"]
    assert ranked[0]["semantic_evaluation"]["status"] == "unknown"
    assert evaluation["failure"] == "semantic_judge_unavailable"


def test_semantic_judge_uses_independent_configured_timeout(monkeypatch):
    captured: dict = {}

    monkeypatch.setattr(
        "wheretogo.research.semantics.get_settings",
        lambda: SimpleNamespace(
            deep_research_semantic_judge_timeout_s=600.0,
        ),
    )

    def fake_extract_json(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "judgments": [{
                "candidate_index": 0,
                "match": True,
                "score": 0.9,
                "supported_criteria": ["是有来源支持的具体餐馆"],
                "contradicted_criteria": [],
                "unknown_criteria": [],
                "reason": "来源支持",
            }],
            "criterion_coverage": 1.0,
            "gaps": [],
        }

    monkeypatch.setattr(
        "wheretogo.research.semantics.extract_json",
        fake_extract_json,
    )

    ranked, evaluation = evaluate_candidates(
        [{
            "title": "示例本帮菜馆",
            "candidate_type": "open_candidate",
            "evidence": {"source_url": "https://example.test/restaurant"},
        }],
        research_goal="推荐有来源支持的上海本帮菜餐馆",
        acceptance_criteria=["是有来源支持的具体餐馆"],
        strict=True,
    )

    assert captured["timeout"] == 600.0
    assert [item["title"] for item in ranked] == ["示例本帮菜馆"]
    assert evaluation["matched_count"] == 1


def test_bare_match_without_supported_hard_criteria_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "wheretogo.research.semantics.extract_json",
        lambda *_args, **_kwargs: {
            "judgments": [{
                "candidate_index": 0,
                "match": True,
                "score": 1.0,
                "supported_criteria": [],
                "contradicted_criteria": [],
                "unknown_criteria": [],
                "reason": "优惠标题提到了千岛湖景区",
            }],
            "criterion_coverage": 1.0,
            "gaps": [],
        },
    )
    ranked, evaluation = evaluate_candidates(
        [{
            "title": "浙BA联赛期间，千岛湖景区免票优惠",
            "candidate_type": "open_candidate",
            "candidate_kind": "促销",
        }],
        research_goal="推荐杭州自然景点",
        acceptance_criteria=["候选本身必须是可到访的自然景点，而非促销资讯"],
        strict=True,
    )
    assert ranked == []
    assert evaluation["matched_count"] == 0


def test_composite_goal_accepts_different_candidates_for_different_subgoals(
    monkeypatch,
):
    monkeypatch.setattr(
        "wheretogo.research.semantics.extract_json",
        lambda *_args, **_kwargs: {
            "judgments": [
                {
                    "candidate_index": 0,
                    "match": True,
                    "score": 0.92,
                    "supported_criteria": ["是可到访的博物馆"],
                    "contradicted_criteria": [],
                    "unknown_criteria": [],
                    "subgoal_assessments": [
                        {
                            "subgoal_id": "museum",
                            "status": "matched",
                            "supported_criteria": ["看博物馆"],
                            "contradicted_criteria": [],
                            "unknown_criteria": [],
                        },
                        {
                            "subgoal_id": "concert",
                            "status": "rejected",
                            "supported_criteria": [],
                            "contradicted_criteria": ["看演唱会"],
                            "unknown_criteria": [],
                        },
                    ],
                    "reason": "覆盖博物馆子目标",
                },
                {
                    "candidate_index": 1,
                    "match": True,
                    "score": 0.9,
                    "supported_criteria": ["日期落在周末的演唱会"],
                    "contradicted_criteria": [],
                    "unknown_criteria": [],
                    "subgoal_assessments": [
                        {
                            "subgoal_id": "museum",
                            "status": "rejected",
                            "supported_criteria": [],
                            "contradicted_criteria": ["看博物馆"],
                            "unknown_criteria": [],
                        },
                        {
                            "subgoal_id": "concert",
                            "status": "matched",
                            "supported_criteria": ["看演唱会"],
                            "contradicted_criteria": [],
                            "unknown_criteria": [],
                        },
                    ],
                    "reason": "覆盖演唱会子目标",
                },
            ],
            "criterion_coverage": 1.0,
            "gaps": [],
        },
    )

    ranked, evaluation = evaluate_candidates(
        [
            {
                "title": "杭州博物馆",
                "candidate_type": "open_candidate",
                "evidence": {"source_url": "https://example.test/museum"},
            },
            {
                "title": "周六音乐现场",
                "candidate_type": "open_candidate",
                "evidence": {"source_url": "https://example.test/concert"},
            },
        ],
        research_goal="周末既看博物馆也看演唱会",
        acceptance_criteria=["是可到访的博物馆", "日期落在周末的演唱会"],
        research_subgoals=[
            {"id": "museum", "objective": "看博物馆", "required": True},
            {"id": "concert", "objective": "看演唱会", "required": True},
        ],
        strict=True,
    )

    assert {item["title"] for item in ranked} == {"杭州博物馆", "周六音乐现场"}
    assert set(evaluation["covered_subgoal_ids"]) == {"museum", "concert"}
    assert evaluation["missing_subgoal_ids"] == []
    assert evaluation["subgoal_coverage"] == 1.0


def test_response_composer_builds_one_itinerary_across_subgoals(monkeypatch):
    monkeypatch.setattr(
        "wheretogo.copilot.respond.extract_json",
        lambda *_args, **_kwargs: None,
    )
    result = compose_research_response(
        {
            "constraints": {
                "research_goal": "周末既看博物馆也看演唱会",
                "research_subgoals": [
                    {"id": "museum", "objective": "看博物馆", "required": True},
                    {"id": "concert", "objective": "看演唱会", "required": True},
                ],
            },
            "research": {"status": "succeeded", "provider_status": "ok"},
            "research_semantic_evaluation": {
                "covered_subgoal_ids": ["museum", "concert"],
                "missing_subgoal_ids": [],
            },
            "activities": [
                {"title": "杭州博物馆", "subgoal_ids": ["museum"]},
                {"title": "周六音乐现场", "subgoal_ids": ["concert"]},
            ],
        }
    )

    assert {
        item["candidate_title"] for item in result["itinerary_draft"]
    } == {"杭州博物馆", "周六音乐现场"}
    assert result["research_context"]["missing_subgoal_ids"] == []
