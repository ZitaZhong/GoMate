"""结构化对话决策与 Deep Research 质量契约的回归测试。"""
from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from wheretogo.bff.app import app
from wheretogo.copilot.handle_turn import handle_turn
from wheretogo.copilot.interpreter import interpret_turn
from wheretogo.db import get_session
from wheretogo.models import Plan, TripBundle
from wheretogo.orchestration.nodes import _assess_research_quality


def test_one_turn_can_update_constraints_and_request_research():
    result = handle_turn(
        "42",
        "再找一批更小众的演唱会",
        memory_ctx={"interests": ["演唱会"]},
        use_llm=False,
    )

    assert result["intent"] == "deep_research"
    assert result["acts"] == ["update_constraints", "research_more"]
    assert result["constraints_patch"]["research_goal"] == "再找一批更小众的演唱会"
    assert result["constraints_patch"]["acceptance_criteria"] == ["再找一批更小众的演唱会"]
    assert "interests" not in result["constraints_patch"]
    assert {command["type"] for command in result["commands"]} == {
        "update_constraints",
        "research_more",
    }
    assert result["turn_decision"]["interpretation_source"] == "rules"


def test_short_answer_is_bound_to_pending_slot():
    result = handle_turn(
        "42",
        "上海",
        memory_ctx={},
        pending_clarify_ctx=[{"slot": "origins", "q": "你从哪里出发？"}],
        use_llm=False,
    )

    assert result["constraints_patch"]["origins"] == ["上海"]
    assert "update_constraints" in result["acts"]


def test_pending_origin_wins_when_model_only_echoes_existing_constraints(monkeypatch):
    """Regression: a city answer must complete the pending origin and start planning."""
    monkeypatch.setattr(
        "wheretogo.copilot.interpreter.extract_json",
        lambda *_args, **_kwargs: {
            "primary_intent": "provide_constraints",
            "acts": ["update_constraints", "clarify"],
            "constraints": {
                "target_city_name": "杭州",
                "weekend_start": "2026-08-08T00:00:00+08:00",
                "weekend_end": "2026-08-10T00:00:00+08:00",
                "experience_requirements": ["看演唱会"],
            },
            "constraint_operations": [],
            "assistant_reply": "收到，您从上海出发。",
            "memory_note": "用户确认从上海出发。",
            "confidence": 0.96,
        },
    )

    result = handle_turn(
        "42",
        "上海",
        memory_ctx={
            "target_city_name": "杭州",
            "target_city_code": "330100",
            "weekend_start": "2026-08-08T00:00:00+08:00",
            "weekend_end": "2026-08-10T00:00:00+08:00",
            "experience_requirements": ["看演唱会"],
        },
        pending_clarify_ctx=[{"slot": "origins", "q": "你们从哪里出发？"}],
        use_llm=True,
    )

    assert result["constraints_patch"]["origins"] == ["上海"]
    assert "target_city_name" not in result["constraints_patch"]
    assert "target_city_code" not in result["constraints_patch"]
    assert result["pending_clarify"] == []


def test_pending_origin_completion_marks_plan_ready_and_requests_stream(monkeypatch):
    monkeypatch.setattr(
        "wheretogo.copilot.interpreter.extract_json",
        lambda *_args, **_kwargs: {
            "primary_intent": "provide_constraints",
            "acts": ["update_constraints", "clarify"],
            "constraints": {
                "target_city_name": "杭州",
                "weekend_start": "2026-08-08T00:00:00+08:00",
                "weekend_end": "2026-08-10T00:00:00+08:00",
                "experience_requirements": ["看演唱会"],
            },
            "assistant_reply": "收到，您从上海出发，我现在开始规划。",
            "confidence": 0.96,
        },
    )
    client = TestClient(app)
    plan_id = client.post(
        "/plans",
        json={
            "constraints": {
                "target_city_name": "杭州",
                "target_city_code": "330100",
                "weekend_start": "2026-08-08T00:00:00+08:00",
                "weekend_end": "2026-08-10T00:00:00+08:00",
                "experience_requirements": ["看演唱会"],
            }
        },
    ).json()["plan_id"]
    with get_session() as session:
        plan = session.get(Plan, int(plan_id))
        plan.conversation = [
            {"role": "user", "content": "下周去杭州看演唱会"},
            {
                "role": "assistant",
                "content": "你们从哪里出发？",
                "pending_clarify": [
                    {"slot": "origins", "q": "你们从哪里出发？"}
                ],
            },
        ]
        session.commit()

    response = client.post(
        f"/plans/{plan_id}/chat",
        json={"message": "上海"},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["constraints"]["origins"] == ["上海"]
    assert body["pending_clarify"] == []
    assert body["ready_to_plan"] is True
    assert body["restart_stream"] is True
    assert body["next_run"]["type"] == "stream"


def test_structured_interpreter_preserves_mixed_question_and_update(monkeypatch):
    monkeypatch.setattr(
        "wheretogo.copilot.interpreter.extract_json",
        lambda *_args, **_kwargs: {
            "primary_intent": "ask_info",
            "acts": ["update_constraints", "answer_info"],
            "constraints": {"target_city_name": "杭州"},
            "constraint_operations": [
                {"op": "set", "field": "target_city_name", "value": "杭州"}
            ],
            "references": ["莫奈展"],
            "confidence": 0.93,
        },
    )
    result = handle_turn(
        "42",
        "目的地改杭州，顺便查一下莫奈展门票",
        memory_ctx={"origins": ["上海"]},
        use_llm=True,
        session=None,
    )

    assert result["intent"] == "provide_constraints"
    assert result["turn_decision"]["primary_intent"] == "ask_info"
    assert result["constraints_patch"]["target_city_name"] == "杭州"
    assert {"update_constraints", "answer_info"} <= set(result["acts"])
    assert result["turn_decision"]["references"] == ["莫奈展"]
    assert {"update_constraints", "answer"} <= {
        item["type"] for item in result["commands"]
    }


def test_structured_remove_operation_is_applied_to_current_constraints(monkeypatch):
    monkeypatch.setattr(
        "wheretogo.copilot.interpreter.extract_json",
        lambda *_args, **_kwargs: {
            "primary_intent": "refine_field",
            "acts": ["update_constraints"],
            "constraints": {},
            "constraint_operations": [
                {"op": "remove", "field": "interests", "value": ["演唱会"]}
            ],
            "confidence": 0.9,
        },
    )
    result = handle_turn(
        "42",
        "把第一类去掉",
        memory_ctx={"interests": ["演唱会", "展览"]},
        use_llm=True,
    )

    assert result["constraints_patch"]["interests"] == ["展览"]


def test_model_answers_from_current_research_without_database_fallback(monkeypatch):
    handle_module = importlib.import_module("wheretogo.copilot.handle_turn")
    monkeypatch.setattr(
        "wheretogo.copilot.interpreter.extract_json",
        lambda *_args, **_kwargs: {
            "primary_intent": "ask_info",
            "acts": ["answer_info"],
            "constraints": {},
            "assistant_reply": "第一次去杭州，我更推荐杭州博物馆，因为它更适合建立城市历史脉络。",
            "memory_note": "用户正在比较现有博物馆候选。",
            "confidence": 0.95,
        },
    )
    monkeypatch.setattr(
        handle_module,
        "_answer_from_db",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("existing-plan questions must not fall back to keyword DB lookup")
        ),
    )

    result = handle_turn(
        "42",
        "这几个里哪个最适合第一次去杭州？",
        memory_ctx={"target_city_name": "杭州"},
        conversation=[
            {"role": "user", "content": "下周去杭州看博物馆"},
            {"role": "assistant", "content": "我找到了几个有来源的候选。"},
        ],
        latest_results={
            "activities": [
                {"title": "杭州博物馆", "description": "展示杭州历史文化"}
            ]
        },
        use_llm=True,
    )

    assert result["reply"].startswith("第一次去杭州")
    assert "answer_info" in result["acts"]
    assert not any(
        command["type"] == "research_more" for command in result["commands"]
    )


def test_interpreter_supplies_recent_turns_and_bounded_long_term_ledger(monkeypatch):
    captured = {}

    def fake_extract(_name, _system, user_payload, **_kwargs):
        captured.update(json.loads(user_payload))
        return {
            "primary_intent": "chitchat",
            "acts": ["chitchat"],
            "constraints": {},
            "assistant_reply": "我们接着规划。",
            "confidence": 0.9,
        }

    monkeypatch.setattr(
        "wheretogo.copilot.interpreter.extract_json",
        fake_extract,
    )
    conversation = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"第 {index + 1} 轮内容",
        }
        for index in range(120)
    ]

    interpret_turn(
        "继续刚才的长线任务",
        fallback_intent="chitchat",
        conversation=conversation,
        use_llm=True,
    )

    memory = captured["conversation_memory"]
    assert memory["total_turns"] == 120
    assert len(memory["recent_turns"]) == 24
    assert memory["recent_turns"][0]["content"] == "第 97 轮内容"
    assert len(memory["earlier_turn_ledger"]) == 80
    assert memory["earlier_turn_ledger"][-1]["content"] == "第 96 轮内容"


def test_explicit_no_search_is_honored_when_turn_model_times_out(monkeypatch):
    monkeypatch.setattr(
        "wheretogo.copilot.interpreter.extract_json",
        lambda *_args, **_kwargs: None,
    )
    itinerary = [
        {
            "day": "周六",
            "time_window": "上午",
            "candidate_title": "浙江省博物馆",
            "reason": "现有安排",
        }
    ]

    result = handle_turn(
        "42",
        "把它改到周日上午，不要重新搜索",
        memory_ctx={
            "target_city_name": "杭州",
            "weekend_start": "2026-08-08T00:00:00+08:00",
        },
        latest_results={
            "activities": [{"title": "浙江省博物馆"}],
            "itinerary_draft": itinerary,
        },
        use_llm=True,
    )

    assert "research_more" not in result["acts"]
    assert result["constraints_patch"] is None
    assert result["itinerary_draft"] == itinerary
    assert "不启动外部搜索" in result["reply"]


def test_existing_plan_can_be_recomposed_without_triggering_research(monkeypatch):
    client = TestClient(app)
    plan_id = client.post(
        "/plans",
        json={
            "constraints": {
                "origins": ["上海"],
                "target_city_code": "330100",
                "weekend_start": "2026-08-08T00:00:00+08:00",
            }
        },
    ).json()["plan_id"]
    with get_session() as session:
        session.add(TripBundle(
            plan_id=int(plan_id),
            version="explore",
            payload={
                "activities": [
                    {"id": 1, "title": "杭州博物馆"},
                    {"id": 2, "title": "中国水利博物馆"},
                ],
                "itinerary_draft": [],
                "assistant_response": "旧方案",
            },
        ))
        session.commit()
    calls = {"prepare": 0}

    class Planner:
        def get_state(self, *_args, **_kwargs):
            return SimpleNamespace(
                values={
                    "constraints": {"target_city_code": "330100"},
                    "activities": [
                        {"id": 1, "title": "杭州博物馆"},
                        {"id": 2, "title": "中国水利博物馆"},
                    ],
                    "itinerary_draft": [],
                }
            )

        def prepare_research_more(self, *_args, **_kwargs):
            calls["prepare"] += 1

    bff = importlib.import_module("wheretogo.bff.app")
    monkeypatch.setattr(bff, "get_planner", lambda: Planner())
    monkeypatch.setattr(
        bff,
        "handle_turn",
        lambda *_args, **_kwargs: {
            "plan_id": plan_id,
            "intent": "ask_info",
            "action": "answer",
            "reply": "已把杭州博物馆安排到周日下午。",
            "constraints_patch": {"target_city_code": "330100"},
            "booking": None,
            "pending_clarify": [],
            "acts": ["recompose_plan"],
            "commands": [{"type": "recompose_plan", "payload": {}}],
            "itinerary_draft": [
                {
                    "day": "周日",
                    "time_window": "下午",
                    "candidate_title": "杭州博物馆",
                    "reason": "按用户要求调整",
                }
            ],
        },
    )

    response = client.post(
        f"/plans/{plan_id}/chat",
        json={"message": "把杭州博物馆放到周日下午，不要重新搜索"},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["reply"] == "已把杭州博物馆安排到周日下午。"
    assert body["itinerary_draft"][0]["time_window"] == "下午"
    assert not body.get("auto_stream")
    assert not body.get("restart_stream")
    assert calls["prepare"] == 0
    with get_session() as session:
        persisted = (
            session.query(TripBundle)
            .filter_by(plan_id=int(plan_id), version="explore")
            .order_by(TripBundle.id.desc())
            .first()
        )
        assert persisted.payload["assistant_response"] == body["reply"]
        assert persisted.payload["itinerary_draft"] == body["itinerary_draft"]


def test_destination_change_restarts_discovery_instead_of_resuming_old_research(
    monkeypatch,
):
    client = TestClient(app)
    created = client.post(
        "/plans",
        json={
            "constraints": {
                "origins": ["上海"],
                "target_city_code": "330100",
                "target_city_name": "杭州",
                "weekend_start": "2026-08-08T00:00:00+08:00",
                "weekend_end": "2026-08-10T00:00:00+08:00",
            }
        },
    ).json()
    plan_id = created["plan_id"]
    with get_session() as session:
        original_thread = session.get(Plan, int(plan_id)).thread_id
    calls = {"prepare": 0}

    class Planner:
        def get_state(self, *_args, **_kwargs):
            return SimpleNamespace(
                values={
                    "constraints": {
                        "origins": ["上海"],
                        "target_city_code": "330100",
                    },
                    "candidate_cities": [
                        {"city_code": "330100", "name": "杭州"}
                    ],
                    "activities": [{"id": 1, "title": "杭州旧候选"}],
                }
            )

        def prepare_research_more(self, *_args, **_kwargs):
            calls["prepare"] += 1

    bff = importlib.import_module("wheretogo.bff.app")
    monkeypatch.setattr(bff, "get_planner", lambda: Planner())
    monkeypatch.setattr(
        bff,
        "handle_turn",
        lambda *_args, **_kwargs: {
            "plan_id": plan_id,
            "intent": "provide_constraints",
            "action": "invoke",
            "reply": "已改为从杭州去上海，重新规划。",
            "constraints_patch": {
                "origins": ["杭州"],
                "target_city_code": "310000",
                "target_city_name": "上海",
                "research_goal": "研究上海的新行程",
                "__research_feedback": "从杭州去上海，重新规划",
            },
            "booking": None,
            "pending_clarify": [],
            "acts": ["update_constraints", "research_more"],
            "commands": [{
                "type": "research_more",
                "payload": {"feedback": "从杭州去上海，重新规划"},
            }],
        },
    )

    response = client.post(
        f"/plans/{plan_id}/chat",
        json={"message": "改成从杭州去上海"},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["restart_stream"] is True
    assert not body.get("auto_stream")
    assert body["next_run"]["type"] == "stream"
    assert calls["prepare"] == 0
    with get_session() as session:
        plan = session.get(Plan, int(plan_id))
        assert plan.constraints["target_city_code"] == "310000"
        assert plan.thread_id != original_thread
        assert body["plan_revision"] == plan.thread_id


def test_booking_extraction_remains_an_unconfirmed_draft():
    result = handle_turn(
        "42",
        "我买好了 G7502 上海虹桥到杭州东，周六 8:00",
        use_llm=False,
    )

    assert result["intent"] == "confirm_booking"
    assert result["booking"]["confirmed"] is False
    assert result["booking"]["ready_for_resume"] is False
    assert "submit_booking_draft" in {
        command["type"] for command in result["commands"]
    }


def test_chat_persists_recent_turns_and_passes_them_back_to_interpreter(monkeypatch):
    client = TestClient(app)
    plan_id = client.post(
        "/plans",
        json={"constraints": {"origins": ["上海"], "interests": ["展览"]}},
    ).json()["plan_id"]
    captured: list[list[dict]] = []
    bff = importlib.import_module("wheretogo.bff.app")

    def _fake_handle(*_args, **kwargs):
        captured.append(list(kwargs.get("conversation") or []))
        return {
            "plan_id": plan_id,
            "intent": "chitchat",
            "action": "answer",
            "reply": "收到",
            "constraints_patch": None,
            "booking": None,
            "pending_clarify": [],
            "acts": ["chitchat"],
            "commands": [],
            "turn_decision": {},
        }

    monkeypatch.setattr(bff, "handle_turn", _fake_handle)
    assert client.post(f"/plans/{plan_id}/chat", json={"message": "第一句"}).status_code == 200
    assert client.post(f"/plans/{plan_id}/chat", json={"message": "还是刚才那个"}).status_code == 200

    assert captured[0] == []
    assert [turn["content"] for turn in captured[1]] == ["第一句", "收到"]
    with get_session() as session:
        stored = session.get(Plan, int(plan_id)).conversation
        assert [turn["content"] for turn in stored] == [
            "第一句",
            "收到",
            "还是刚才那个",
            "收到",
        ]

    state = client.get(f"/plans/{plan_id}/agent-state")
    assert state.status_code == 200
    assert [turn["content"] for turn in state.json()["conversation"]] == [
        "第一句",
        "收到",
        "还是刚才那个",
        "收到",
    ]


def test_weather_command_describes_replan_without_claiming_it_already_happened():
    result = handle_turn(
        "42",
        "周日有暴雨，行程要不要调整",
        use_llm=False,
    )

    assert result["intent"] == "weather"
    assert "request_weather_replan" in {
        command["type"] for command in result["commands"]
    }
    assert "还没有擅自改动" in result["reply"]


def test_research_quality_uses_entities_evidence_and_source_metrics():
    activities = [
        {"id": i, "title": f"活动{i}", "evidence": {"source_url": f"https://x/{i}"}}
        for i in range(1, 4)
    ]
    quality = _assess_research_quality(
        {
            "research": {
                "status": "succeeded",
                "source_count": 8,
                "official_count": 2,
                "query_count": 6,
                "round_count": 1,
                "coverage": 0.9,
                "marginal_gain": 0.375,
                "termination": "completed",
            }
        },
        activities,
    )

    assert quality["sufficient"] is True
    assert quality["distinct_entity_count"] == 3
    assert quality["source_count"] == 8
    assert quality["official_count"] == 2
    assert quality["coverage"] == 0.9


def test_research_quality_rejects_source_count_without_actionable_entities():
    quality = _assess_research_quality(
        {
            "research": {
                "status": "no_results",
                "source_count": 30,
                "coverage": 1.0,
                "termination": "converged",
            }
        },
        [],
    )

    assert quality["sufficient"] is False
    assert "distinct_entities" in quality["gaps"]
    assert quality["termination"] == "converged"
