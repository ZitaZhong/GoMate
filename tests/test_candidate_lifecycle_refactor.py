"""Regression coverage for candidate lifecycle and partial semantic review."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from wheretogo.copilot.respond import compose_research_response
from wheretogo.orchestration import nodes
from wheretogo.research.semantics import (
    evaluate_candidates,
    extract_open_candidates,
)
from wheretogo.retrieval import Weekend


def _open_candidate(title: str, subgoal_id: str = "food") -> dict:
    return {
        "id": None,
        "title": title,
        "candidate_type": "open_candidate",
        "candidate_kind": "来源中的具体候选",
        "description": "公开来源支持的候选事实",
        "availability_mode": "recurring",
        "subgoal_ids": [subgoal_id],
        "verification_status": "public_source_observed",
        "evidence": {
            "source_type": "search",
            "source_url": f"https://example.test/{title}",
            "verification_status": "public_source_observed",
        },
    }


def test_semantic_batches_keep_source_backed_candidates_from_failed_batch(
    monkeypatch,
):
    calls: list[dict] = []
    monkeypatch.setattr(
        "wheretogo.research.semantics.get_settings",
        lambda: SimpleNamespace(
            deep_research_semantic_judge_timeout_s=600.0,
            deep_research_semantic_judge_batch_size=10,
            deep_research_semantic_judge_concurrency=2,
        ),
    )

    def fake_extract_json(_task, _instruction, payload, **kwargs):
        body = json.loads(payload)
        batch = body["candidates"]
        calls.append({"indices": [item["candidate_index"] for item in batch], **kwargs})
        if batch[0]["candidate_index"] == 10:
            return None
        return {
            "judgments": [
                {
                    "candidate_index": item["candidate_index"],
                    "match": True,
                    "score": 0.9,
                    "supported_criteria": ["是来源支持的具体候选"],
                    "contradicted_criteria": [],
                    "unknown_criteria": [],
                    "reason": "来源支持",
                }
                for item in batch
            ],
            "criterion_coverage": 1.0,
            "gaps": [],
        }

    monkeypatch.setattr(
        "wheretogo.research.semantics.extract_json",
        fake_extract_json,
    )
    candidates = [_open_candidate(f"候选{i}", "goal_1") for i in range(12)]

    ranked, evaluation = evaluate_candidates(
        candidates,
        research_goal="寻找来源支持的具体候选",
        acceptance_criteria=["是来源支持的具体候选"],
        strict=True,
    )

    assert len(calls) == 2
    assert {call["timeout"] for call in calls} == {600.0}
    assert evaluation["failure"] == "semantic_judge_partial"
    assert evaluation["successful_batch_count"] == 1
    assert evaluation["failed_batch_count"] == 1
    assert len(ranked) == 12
    pending = [
        item for item in ranked
        if item["semantic_evaluation"]["status"] == "unknown"
    ]
    assert {item["title"] for item in pending} == {"候选10", "候选11"}


def test_extend_turn_preserves_itinerary_and_never_loses_fresh_candidates_on_judge_outage(
    monkeypatch,
):
    baseline = [
        {
            "id": index,
            "title": title,
            "candidate_type": "open_candidate",
            "evidence": {
                "source_type": "search",
                "source_url": f"https://old.example/{index}",
            },
        }
        for index, title in enumerate(
            [
                "上海市历史博物馆",
                "新天地",
                "世博会博物馆",
                "外滩",
                "上海博物馆",
                "豫园",
                "上海中心",
                "东方明珠",
                "共青森林公园",
                "滨江森林公园",
            ],
            start=1,
        )
    ]
    restaurants = [
        _open_candidate(title)
        for title in [
            "人和馆",
            "老吉士酒家",
            "上海老饭店",
            "光明邨大酒家",
            "兰心餐厅",
            "大壶春",
        ]
    ]

    @contextmanager
    def fake_session():
        yield object()

    class EmptyRetrieval:
        def retrieve_activities(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr(nodes, "get_session", fake_session)
    monkeypatch.setattr(nodes, "_city_name", lambda *_args: "上海")
    monkeypatch.setattr(nodes, "_RETRIEVAL", EmptyRetrieval())
    monkeypatch.setattr(nodes._deep_research, "enabled", lambda: True)
    monkeypatch.setattr(nodes._deep_research, "should_run", lambda: True)
    monkeypatch.setattr(
        nodes._deep_research,
        "run",
        lambda *_args, **_kwargs: {
            "activity_ids": [],
            "candidates": restaurants,
            "job_id": None,
            "status": "succeeded",
            "source_count": 12,
            "official_count": 1,
            "termination": "completed",
            "query_count": 4,
            "round_count": 1,
            "coverage": 1.0,
            "marginal_gain": 6.0,
            "trace": {},
            "provider_status": "ok",
            "provider_errors": [],
        },
    )

    def unavailable_judge(values, **_kwargs):
        pending = []
        for value in values:
            pending.append({
                **value,
                "semantic_evaluation": {
                    "status": "unknown",
                    "reason": "语义评审暂不可用",
                },
            })
        return pending, {
            "evaluated": False,
            "matched_count": 0,
            "criterion_coverage": 0.0,
            "gaps": ["餐馆与特色菜仍待语义复核"],
            "failure": "semantic_judge_unavailable",
            "covered_subgoal_ids": [],
            "missing_subgoal_ids": ["route", "food"],
            "subgoal_coverage": 0.0,
        }

    monkeypatch.setattr(
        "wheretogo.research.semantics.evaluate_candidates",
        unavailable_judge,
    )
    state = {
        "plan_id": "4069",
        "constraints": {
            "weekend_start": "2026-08-08T00:00:00+08:00",
            "weekend_end": "2026-08-10T00:00:00+08:00",
            "research_goal": "保留既有路线并增加上海当地特色餐馆",
            "acceptance_criteria": ["推荐有来源支持的具体餐馆"],
            "research_subgoals": [
                {
                    "id": "route",
                    "objective": "保留现有四个地点",
                    "required": True,
                    "acceptance_criteria": ["保留既有行程"],
                },
                {
                    "id": "food",
                    "objective": "推荐上海当地特色餐馆",
                    "required": True,
                    "acceptance_criteria": ["是有来源支持的具体餐馆"],
                },
            ],
        },
        "candidate_cities": [{"city_code": "310000", "name": "上海"}],
        "activities": baseline,
        "itinerary_draft": [
            {"candidate_title": title}
            for title in ["上海市历史博物馆", "新天地", "世博会博物馆", "外滩"]
        ],
        "research_baseline_activities": baseline,
        "research_round_candidates": [],
        "research_raw_candidates": [],
        "research_judged_candidates": [],
        "research_revision_mode": "extend",
        "research_active_feedback": "我还要吃当地特色美食，推荐几家餐馆",
        "follow_up_queries": ["上海 当地特色美食 餐馆"],
        "shown_activity_ids": [item["id"] for item in baseline],
        "shown_activity_titles": [item["title"] for item in baseline],
    }

    researched = nodes.activity_research(state)

    selected_titles = [item["title"] for item in researched["activities"]]
    assert selected_titles[:4] == [
        "上海市历史博物馆",
        "新天地",
        "世博会博物馆",
        "外滩",
    ]
    assert set(selected_titles[4:]) == {item["title"] for item in restaurants}
    assert researched["research_selection"]["selected_fresh_count"] == 6
    assert researched["research_selection"]["preserved_baseline_count"] == 4
    assert researched["research_selection"]["candidate_loss_anomaly"] is False
    assert researched["research_improved"] is False
    assert researched["research_outcome"] == "partial_unverified"
    assert len(researched["research_raw_candidates"]) == 6

    reflected = nodes.activity_reflection({**state, **researched})
    assert reflected["research_outcome"] == "partial_unverified"
    assert len(reflected["research_raw_candidates"]) == 6
    assert len(reflected["research_round_candidates"]) == 6

    monkeypatch.setattr(
        "wheretogo.copilot.respond.extract_json",
        lambda *_args, **_kwargs: None,
    )
    response = compose_research_response({**state, **researched, **reflected})
    assert response["research_context"]["raw_candidate_count"] == 6
    assert response["plan_delta"]["preserve"] == selected_titles[:4]
    assert set(response["plan_delta"]["add"]) == set(selected_titles[4:])
    assert "待复核" in response["assistant_response"]


def test_frontend_distinguishes_partial_candidates_from_verified_matches():
    html = Path("web/index.html").read_text(encoding="utf-8")
    assert 'researchOutcome === "partial_unverified"' in html
    assert 'data-semantic-status="unknown"' in html
    assert "部分语义或营业信息仍待复核" in html


def test_recurring_availability_is_kept_with_date_confirmation_separated(
    monkeypatch,
):
    monkeypatch.setattr(
        "wheretogo.research.semantics.extract_json",
        lambda *_args, **_kwargs: {
            "candidates": [{
                "title": "来源中的具体去处",
                "kind": "开放文本类型",
                "summary": "每周六日开放",
                "venue": "某地址",
                "source_index": 0,
                "subgoal_ids": ["goal_1"],
                "availability": {
                    "mode": "recurring",
                    "recurring_hours": ["周六、周日 10:00-20:00"],
                },
            }],
        },
    )
    candidates = extract_open_candidates(
        [{
            "title": "公开来源",
            "url": "https://example.test/source",
            "content": "来源正文",
        }],
        {
            "research_goal": "寻找具体去处",
            "research_subgoals": [{
                "id": "goal_1",
                "objective": "寻找具体去处",
                "acceptance_criteria": ["有来源支持"],
                "required": True,
            }],
        },
    )

    availability = candidates[0]["availability"]
    assert availability["mode"] == "recurring"
    assert availability["date_specific_status"] == "no_known_conflict"
    assert availability["recurring_hours"] == ["周六、周日 10:00-20:00"]
    assert candidates[0]["claims"][1]["field"] == "availability"

    contradicted = {
        **candidates[0],
        "availability": {
            **availability,
            "date_specific_status": "contradicted",
        },
    }
    weekend = Weekend(
        datetime(2026, 8, 8, tzinfo=timezone.utc),
        datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    assert nodes._candidate_available(candidates[0], weekend) is True
    assert nodes._candidate_available(contradicted, weekend) is False


def test_candidate_relevance_and_collection_coverage_are_separate(monkeypatch):
    criteria = ["保留地点甲", "保留地点乙", "保留地点丙", "保留地点丁"]

    def fake_extract_json(_task, instruction, payload, **_kwargs):
        assert "not whole-plan completeness" in instruction
        body = json.loads(payload)
        judgments = []
        for candidate in body["candidates"]:
            index = candidate["candidate_index"]
            supported = criteria[index]
            judgments.append({
                "candidate_index": index,
                "match": True,
                "score": 0.9,
                "supported_criteria": [supported],
                "contradicted_criteria": [],
                "unknown_criteria": [
                    value for value in criteria if value != supported
                ],
                "reason": "该候选是完整行程的一个来源支持组件",
                "subgoal_assessments": [{
                    "subgoal_id": "route",
                    "status": "matched",
                    "supported_criteria": [supported],
                    "contradicted_criteria": [],
                    "unknown_criteria": [
                        value for value in criteria if value != supported
                    ],
                }],
            })
        return {
            "judgments": judgments,
            "criterion_coverage": 0.25,
            "gaps": criteria[1:],
        }

    monkeypatch.setattr(
        "wheretogo.research.semantics.extract_json",
        fake_extract_json,
    )
    candidates = [
        _open_candidate(title, "route")
        for title in ["地点甲", "地点乙", "地点丙", "地点丁"]
    ]

    ranked, evaluation = evaluate_candidates(
        candidates,
        research_goal="把四个指定地点组成一条完整路线",
        acceptance_criteria=criteria,
        research_subgoals=[{
            "id": "route",
            "objective": "保留四个指定地点",
            "acceptance_criteria": criteria,
            "required": True,
            "target_count": 4,
        }],
        strict=True,
    )

    assert {item["title"] for item in ranked} == {
        "地点甲", "地点乙", "地点丙", "地点丁",
    }
    assert evaluation["matched_count"] == 4
    assert evaluation["covered_subgoal_ids"] == ["route"]
    assert evaluation["missing_subgoal_ids"] == []
    assert evaluation["criterion_coverage"] == 1.0


def test_target_count_participates_in_research_sufficiency():
    quality = nodes._assess_research_quality(
        {
            "constraints": {
                "research_subgoals": [{
                    "id": "food",
                    "objective": "推荐几家餐馆",
                    "acceptance_criteria": ["是来源支持的具体候选"],
                    "required": True,
                    "target_count": 4,
                }],
            },
            "research": {
                "status": "succeeded",
                "source_count": 8,
                "coverage": 1.0,
            },
            "research_semantic_evaluation": {
                "evaluated": True,
                "matched_count": 3,
                "criterion_coverage": 1.0,
                "gaps": [],
            },
        },
        [_open_candidate(f"餐馆{i}") for i in range(3)],
    )

    assert quality["sufficient"] is False
    assert "distinct_entities" in quality["gaps"]


def test_open_candidate_extraction_uses_dedicated_timeout(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "wheretogo.research.semantics.get_settings",
        lambda: SimpleNamespace(
            deep_research_candidate_extract_timeout_s=180.0,
        ),
    )

    def fake_extract_json(*_args, **kwargs):
        captured.update(kwargs)
        return {"candidates": []}

    monkeypatch.setattr(
        "wheretogo.research.semantics.extract_json",
        fake_extract_json,
    )
    extract_open_candidates(
        [{
            "title": "来源",
            "url": "https://example.test/source",
            "content": "正文",
        }],
        {"research_goal": "寻找来源支持的具体候选"},
    )

    assert captured["timeout"] == 180.0


def test_collection_coverage_survives_a_later_empty_batch():
    baseline = [
        {
            **_open_candidate(title, subgoal_id),
            "semantic_evaluation": {
                "status": "matched",
                "matched_subgoal_ids": [subgoal_id],
            },
        }
        for title, subgoal_id in [
            ("地点甲", "place_a"),
            ("地点乙", "place_b"),
        ]
    ]
    restaurants = [
        {
            **_open_candidate(f"餐馆{i}", "food"),
            "semantic_evaluation": {
                "status": "matched",
                "matched_subgoal_ids": ["food"],
            },
        }
        for i in range(3)
    ]
    subgoals = [
        {
            "id": "place_a",
            "objective": "保留地点甲",
            "required": True,
            "target_count": 1,
        },
        {
            "id": "place_b",
            "objective": "保留地点乙",
            "required": True,
            "target_count": 1,
        },
        {
            "id": "food",
            "objective": "推荐三家餐馆",
            "required": True,
            "target_count": 3,
        },
    ]

    evaluation = nodes._collection_semantic_evaluation(
        [*baseline, *restaurants],
        subgoals,
        {
            "evaluated": False,
            "matched_count": 0,
            "criterion_coverage": 0.0,
            "covered_subgoal_ids": [],
            "missing_subgoal_ids": ["place_a", "place_b", "food"],
            "gaps": ["empty latest batch"],
        },
    )

    assert evaluation["matched_count"] == 5
    assert evaluation["covered_subgoal_ids"] == ["food", "place_a", "place_b"]
    assert evaluation["missing_subgoal_ids"] == []
    assert evaluation["criterion_coverage"] == 1.0
    assert evaluation["gaps"] == []


def test_composer_fallback_keeps_each_subgoal_target_count(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "wheretogo.copilot.respond.get_settings",
        lambda: SimpleNamespace(
            trip_response_compose_timeout_s=600.0,
        ),
    )

    def unavailable_composer(*_args, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(
        "wheretogo.copilot.respond.extract_json",
        unavailable_composer,
    )
    places = [
        _open_candidate(title, subgoal_id)
        for title, subgoal_id in [
            ("地点甲", "place_a"),
            ("地点乙", "place_b"),
            ("地点丙", "place_c"),
            ("地点丁", "place_d"),
        ]
    ]
    restaurants = [
        _open_candidate(f"餐馆{i}", "food")
        for i in range(3)
    ]
    result = compose_research_response({
        "constraints": {
            "research_subgoals": [
                {
                    "id": subgoal_id,
                    "objective": f"保留{title}",
                    "required": True,
                    "target_count": 1,
                }
                for title, subgoal_id in [
                    ("地点甲", "place_a"),
                    ("地点乙", "place_b"),
                    ("地点丙", "place_c"),
                    ("地点丁", "place_d"),
                ]
            ] + [{
                "id": "food",
                "objective": "推荐三家餐馆",
                "required": True,
                "target_count": 3,
            }],
        },
        "activities": [*places, *restaurants],
    })

    itinerary_titles = [
        item["candidate_title"] for item in result["itinerary_draft"]
    ]
    assert itinerary_titles == [
        "地点甲", "地点乙", "地点丙", "地点丁",
        "餐馆0", "餐馆1", "餐馆2",
    ]
    assert captured["timeout"] == 600.0


def test_composer_repairs_invalid_or_incomplete_model_itinerary(monkeypatch):
    monkeypatch.setattr(
        "wheretogo.copilot.respond.get_settings",
        lambda: SimpleNamespace(
            trip_response_compose_timeout_s=600.0,
        ),
    )
    places = [
        {
            **_open_candidate(title, subgoal_id),
            "origin": "baseline",
        }
        for title, subgoal_id in [
            ("地点甲", "place_a"),
            ("地点乙", "place_b"),
            ("地点丙", "place_c"),
            ("地点丁", "place_d"),
        ]
    ]
    restaurants = [
        {
            **_open_candidate(f"餐馆{i}", "food"),
            "origin": "current_research",
        }
        for i in range(3)
    ]
    subgoals = [
        {
            "id": subgoal_id,
            "objective": f"保留{title}",
            "required": True,
            "target_count": 1,
        }
        for title, subgoal_id in [
            ("地点甲", "place_a"),
            ("地点乙", "place_b"),
            ("地点丙", "place_c"),
            ("地点丁", "place_d"),
        ]
    ] + [{
        "id": "food",
        "objective": "推荐三家餐馆",
        "required": True,
        "target_count": 3,
    }]
    monkeypatch.setattr(
        "wheretogo.copilot.respond.extract_json",
        lambda *_args, **_kwargs: {
            "reply": "已安排餐馆0、餐馆1和不存在的餐馆。",
            "itinerary_draft": [
                {
                    "day": "周末",
                    "time_window": "待确认",
                    "candidate_title": item["title"],
                    "reason": "模型安排",
                }
                for item in [*places, *restaurants[:2]]
            ] + [{
                "day": "周末",
                "time_window": "待确认",
                "candidate_title": "不存在的餐馆",
                "reason": "无证据",
            }],
            "plan_delta": {},
        },
    )

    result = compose_research_response({
        "constraints": {"research_subgoals": subgoals},
        "activities": [*places, *restaurants],
    })
    itinerary_titles = [
        item["candidate_title"] for item in result["itinerary_draft"]
    ]

    assert itinerary_titles == [
        "地点甲", "地点乙", "地点丙", "地点丁",
        "餐馆0", "餐馆1", "餐馆2",
    ]
    assert "不存在的餐馆" not in result["assistant_response"]
    assert "餐馆0、餐馆1、餐馆2" in result["assistant_response"]
