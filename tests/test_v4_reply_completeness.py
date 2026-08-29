"""v4 回复自洽化 + 天气正确性（S8 暴露的产品级问题回归）。

产品目标：聊天回复正文本身即完整上下文（每项日期/时段/地点/理由/证据），
卡片仅为可选辅助。全部离线确定性（monkeypatch extract_json），不联网。
"""
from __future__ import annotations

import uuid

import pytest

from wheretogo.agent.status import RunStatus, TurnStatus
from wheretogo.agent.supervisor import execute_run
from wheretogo.agent.transaction import commit_turn
from wheretogo.copilot.respond import (
    _inline_schedule,
    compose_confirm_reply,
    compose_research_response,
)
from wheretogo.copilot.turn_schema import TurnDecision
from wheretogo.models import AgentOutbox, AgentTurn, Plan, TripBundle

_POINTER_PHRASES = ["见下方", "见卡片", "如下卡片", "详情见下方"]


def _assert_no_pointer(text: str) -> None:
    for phrase in _POINTER_PHRASES:
        assert phrase not in text, f"回复不应把细节推给卡片，命中指针措辞：{phrase}"


# ---------- 1. 内联排期 helper ----------

def test_inline_schedule_carries_day_time_venue_reason_evidence():
    itinerary = [
        {"day": "周六", "time_window": "上午", "candidate_title": "上海博物馆", "reason": "文化地标"},
    ]
    activities = [
        {"title": "上海博物馆", "venue": "人民广场", "verification_status": "public_source_observed"},
    ]
    text = _inline_schedule(itinerary, activities)
    assert "周六" in text and "上午" in text
    assert "上海博物馆" in text and "人民广场" in text
    assert "文化地标" in text
    assert "公开来源待核实" in text  # 证据状态内联


# ---------- 2. 正常研究回复：自洽、无指针 ----------

def _compose_state(activities, extract_return, **extra):
    state = {
        "constraints": {"research_subgoals": []},
        "activities": activities,
        **extra,
    }
    return state, extract_return


def test_research_reply_is_self_contained_without_pointer(monkeypatch):
    activities = [
        {"title": "豫园", "venue": "黄浦区", "subgoal_ids": [], "verification_status": "public_source_observed"},
        {"title": "外滩", "venue": "中山东一路", "subgoal_ids": [], "verification_status": "public_source_observed"},
    ]
    monkeypatch.setattr(
        "wheretogo.copilot.respond.extract_json",
        lambda *_a, **_k: {
            "reply": "为你规划了豫园与外滩的一日安排。",
            "itinerary_draft": [
                {"day": "周六", "time_window": "上午", "candidate_title": "豫园", "reason": "上午游园"},
                {"day": "周六", "time_window": "傍晚", "candidate_title": "外滩", "reason": "看夜景"},
            ],
            "plan_delta": {},
        },
    )
    result = compose_research_response({
        "constraints": {"research_subgoals": []},
        "activities": activities,
    })
    reply = result["assistant_response"]
    # 未触发修复时用模型自然回复；本例模型回复本身自洽
    assert reply and "豫园" in reply
    _assert_no_pointer(reply)


def test_compose_payload_includes_weather_and_prompt_forbids_pointer(monkeypatch):
    captured = {}

    def fake_extract(task, instruction, text, *a, **k):
        captured["instruction"] = instruction
        captured["text"] = text
        return {"reply": "已按室内优先调整。", "itinerary_draft": [], "plan_delta": {}}

    monkeypatch.setattr("wheretogo.copilot.respond.extract_json", fake_extract)
    compose_research_response({
        "constraints": {"research_subgoals": []},
        "activities": [{"title": "某馆", "subgoal_ids": []}],
        "weather": {"adverse": True, "indoor_pref": True, "detail": "暴雨"},
        "replan_reason": "周六暴雨",
    })
    # 提示词明确禁止指针措辞、要求内联
    assert "见下方" in captured["instruction"] or "pointer" in captured["instruction"].lower()
    # payload 携带天气信号
    assert "暴雨" in captured["text"]


# ---------- 3. 修复分支：自洽 + 守住既有红线 ----------

def test_repaired_reply_is_self_contained_and_keeps_titles(monkeypatch):
    places = [{"title": f"地点{c}", "venue": "某区", "subgoal_ids": ["g_place"]} for c in "甲乙"]
    restaurants = [
        {"title": f"餐馆{i}", "venue": "某街", "subgoal_ids": ["g_food"]} for i in range(3)
    ]
    subgoals = [
        {"id": "g_place", "objective": "地点", "required": True, "target_count": 2},
        {"id": "g_food", "objective": "餐馆", "required": True, "target_count": 3},
    ]
    monkeypatch.setattr(
        "wheretogo.copilot.respond.extract_json",
        lambda *_a, **_k: {
            "reply": "已安排餐馆0、餐馆1和不存在的餐馆。",  # 含未接地标题
            "itinerary_draft": [
                {"day": "周末", "time_window": "待确认", "candidate_title": t["title"], "reason": "安排"}
                for t in [*places, restaurants[0], restaurants[1]]
            ] + [{"day": "周末", "time_window": "待确认", "candidate_title": "不存在的餐馆", "reason": "无证据"}],
            "plan_delta": {},
        },
    )
    result = compose_research_response({
        "constraints": {"research_subgoals": subgoals},
        "activities": [*places, *restaurants],
    })
    reply = result["assistant_response"]
    # 守住既有红线：新增标题串在、未接地标题不在
    assert "餐馆0、餐馆1、餐馆2" in reply
    assert "不存在的餐馆" not in reply
    # 新目标：自洽、无指针、含内联排期
    _assert_no_pointer(reply)
    assert "具体安排如下" in reply and "餐馆2" in reply


# ---------- 4. confirm 版自洽回复 ----------

def test_compose_confirm_reply_from_timeline_is_self_contained():
    state = {
        "timeline": [
            {"kind": "transport", "title": "杭州东→上海虹桥", "start_at": "2026-08-01T08:05:00+08:00"},
            {"kind": "activity", "title": "上海博物馆", "start_at": "2026-08-01T10:00:00+08:00",
             "end_at": "2026-08-01T12:00:00+08:00"},
        ],
        "weather": {"adverse": True, "detail": "暴雨"},
        "bookings": [{"confirmed": True, "extracted": {"train_no": "G7503"}}],
    }
    reply = compose_confirm_reply(state)
    assert "上海博物馆" in reply and "10:00" in reply
    assert "暴雨" in reply and "室内" in reply  # 正面回应天气
    assert "G7503" in reply  # 已确认回填内联
    _assert_no_pointer(reply)


def test_confirm_bundle_carries_assistant_response():
    from wheretogo.orchestration.bundle import compose_confirm_bundle
    bundle = compose_confirm_bundle({
        "plan_id": "1",
        "constraints": {},
        "assistant_response": "确认版自洽回复",
    })
    assert bundle["assistant_response"] == "确认版自洽回复"
    # 既有字段仍在（守住 test_regression_v3_fixes）
    assert "cost" in bundle and "risks" in bundle and "alternatives" in bundle


# ---------- 5. recompose 回复自洽（DB 级，共库隔离） ----------

@pytest.fixture(autouse=True)
def _isolate(session):
    session.query(AgentOutbox).filter(AgentOutbox.status == "pending").update(
        {AgentOutbox.status: "done"}, synchronize_session=False
    )
    session.flush()
    yield


def _decision(**overrides) -> TurnDecision:
    values = dict(
        primary_intent="ask_info",
        acts=["recompose_plan"],
        constraints_patch={},
        goals=[{"id": "g1", "objective": "重排", "required": True}],
        proposed_actions=[{"type": "compose_itinerary", "reason": "本地重排"}],
        itinerary_draft=[
            {"day": "周六", "time_window": "上午", "candidate_title": "豫园", "reason": "上午游园"},
        ],
        interpretation_source="rules",
    )
    values.update(overrides)
    return TurnDecision(**values)


def test_recompose_reply_is_self_contained(session, monkeypatch):
    # 预置已有 explore bundle（含候选）
    plan = Plan(stage="await_booking", thread_id=f"pending-{uuid.uuid4()}",
                constraints={"target_city_name": "上海"})
    session.add(plan)
    session.flush()
    plan.thread_id = f"plan:{plan.id}"
    session.add(TripBundle(plan_id=plan.id, version="explore", payload={
        "assistant_response": "已有方案。",
        "activities": [{"title": "豫园", "venue": "黄浦区"}],
        "itinerary_draft": [
            {"day": "周六", "time_window": "上午", "candidate_title": "豫园", "reason": "上午游园"},
        ],
    }))
    session.flush()
    monkeypatch.setattr(
        "wheretogo.agent.transaction.interpret_turn", lambda *a, **k: _decision()
    )
    out = commit_turn(str(plan.id), "不要搜索，就把豫园重排上午", session=session)
    assert out["run"]["type"] == "recompose"
    status = execute_run(out["run"]["id"], session=session, planner=None)
    assert status == RunStatus.SUCCEEDED.value
    turn = session.get(AgentTurn, uuid.UUID(out["turn_id"]))
    assert turn.status == TurnStatus.ANSWERED.value
    assert "豫园" in turn.visible_reply and "未做外部搜索" in turn.visible_reply
    _assert_no_pointer(turn.visible_reply)
