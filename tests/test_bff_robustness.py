"""BFF 边界、幂等和并发保护回归测试。"""
from __future__ import annotations

import importlib
import json
from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from wheretogo.bff.app import app
from wheretogo.db import get_session
from wheretogo.infra.redis_client import plan_lock
from wheretogo.models import PartyConstraint, PlanMember

app_module = importlib.import_module("wheretogo.bff.app")
client = TestClient(app)


def _new_plan(constraints: dict | None = None) -> str:
    response = client.post("/plans", json={"constraints": constraints or {}})
    assert response.status_code == 200
    return response.json()["plan_id"]


@pytest.mark.parametrize("message", ["", " ", "\n\t"])
def test_chat_rejects_blank_messages(message: str):
    plan_id = _new_plan()
    assert client.post(f"/plans/{plan_id}/chat", json={"message": message}).status_code == 422


def test_chat_rejects_unbounded_payload():
    plan_id = _new_plan()
    assert client.post(
        f"/plans/{plan_id}/chat",
        json={"message": "展" * 4001},
    ).status_code == 422


def test_only_new_is_a_valid_non_numeric_chat_plan_id():
    assert client.post("/plans/not-a-plan/chat", json={"message": "你好"}).status_code == 404


def test_research_more_rejects_an_unprepared_fresh_plan():
    plan_id = _new_plan()
    assert client.post(f"/plans/{plan_id}/research-more").status_code == 409


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/plans/999999999/revise"),
        ("post", "/plans/999999999/invites"),
        ("get", "/plans/999999999/party/aggregate"),
    ],
)
def test_all_plan_scoped_routes_reject_unknown_plan(method: str, path: str):
    payload = {"values": {}} if path.endswith("/revise") else {"count": 1}
    response = getattr(client, method)(path, json=payload) if method == "post" else client.get(path)
    assert response.status_code == 404


@pytest.mark.parametrize("count", [0, -1, 9, 100])
def test_invite_count_has_contract_bounds(count: int):
    plan_id = _new_plan()
    assert client.post(f"/plans/{plan_id}/invites", json={"count": count}).status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"earliest_depart": "明天早上"},
        {"budget_band": {"max": True}},
        {"budget_band": {"min": -1, "max": 1000}},
        {"budget_band": {"min": 2000, "max": 1000}},
        {
            "earliest_depart": "2026-08-03T10:00:00+08:00",
            "latest_return": "2026-08-02T18:00:00+08:00",
        },
        {
            "earliest_depart": "2026-08-01T10:00:00",
            "latest_return": "2026-08-02T18:00:00+08:00",
        },
    ],
)
def test_member_constraints_reject_invalid_dates_and_budgets(payload: dict):
    plan_id = _new_plan()
    token = client.post(f"/plans/{plan_id}/invites", json={"count": 1}).json()["invites"][0]["token"]
    assert client.post(f"/invite/{token}/constraints", json=payload).status_code == 422


@pytest.mark.parametrize("amount", [float("inf"), float("-inf"), float("nan")])
def test_member_constraints_reject_non_finite_budget_values(amount: float):
    with pytest.raises(ValidationError):
        app_module.MemberConstraintsBody(budget_band={"max": amount})


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/replan", {"reason": "", "from_node": "dining"}),
        ("/replan", {"reason": "天气变化", "from_node": "not-a-node"}),
        ("/revise", {"values": {}, "from_node": "not-a-node"}),
        ("/bookings/import", {"kind": "bus", "input_kind": "manual"}),
        ("/bookings/import", {"kind": "train", "input_kind": "shell"}),
    ],
)
def test_mutating_routes_reject_invalid_contract_values(path: str, payload: dict):
    plan_id = _new_plan()
    assert client.post(f"/plans/{plan_id}{path}", json=payload).status_code == 422


def test_member_constraint_resubmission_is_an_upsert():
    plan_id = _new_plan()
    invite = client.post(f"/plans/{plan_id}/invites", json={"count": 1}).json()["invites"][0]
    token = invite["token"]

    first = client.post(
        f"/invite/{token}/constraints",
        json={"origin_area": "上海·徐汇", "accept_flight": False, "interests": ["展览"]},
    )
    second = client.post(
        f"/invite/{token}/constraints",
        json={"origin_area": "上海·浦东", "accept_flight": True, "interests": ["演出"]},
    )
    assert first.status_code == second.status_code == 200

    with get_session() as session:
        member = session.query(PlanMember).filter_by(invite_token=token).one()
        rows = session.query(PartyConstraint).filter_by(member_id=member.id).all()
        assert len(rows) == 1
        assert rows[0].origin_area == "上海·浦东"
        assert rows[0].prefer_flight is True
        assert rows[0].prefs == ["演出"]

    aggregate = client.get(f"/plans/{plan_id}/party/aggregate").json()
    assert aggregate["members"] == 1


def test_refine_interest_response_requests_a_new_stream(monkeypatch):
    plan_id = _new_plan({"origins": ["上海"], "interests": ["演唱会"]})

    def fake_handle_turn(*_args, **_kwargs):
        return {
            "plan_id": plan_id,
            "intent": "refine_field",
            "action": "update_state",
            "reply": "已更新",
            "constraints_patch": {"interests": ["展览"]},
            "booking": None,
            "pending_clarify": [],
        }

    monkeypatch.setattr(app_module, "handle_turn", fake_handle_turn)
    response = client.post(f"/plans/{plan_id}/chat", json={"message": "我想改看展览"})
    assert response.status_code == 200
    data = response.json()
    assert data["restart_stream"] is True
    assert data["constraints"]["interests"] == ["展览"]


def test_removing_the_only_interest_restarts_as_all_categories(monkeypatch):
    plan_id = _new_plan({"origins": ["上海"], "interests": ["演唱会"]})
    monkeypatch.setattr(
        app_module,
        "handle_turn",
        lambda *_args, **_kwargs: {
            "plan_id": plan_id,
            "intent": "refine_field",
            "action": "update_state",
            "reply": "已移除",
            "constraints_patch": {"interests": []},
            "booking": None,
            "pending_clarify": [],
        },
    )
    data = client.post(f"/plans/{plan_id}/chat", json={"message": "不看演唱会了"}).json()
    assert data["restart_stream"] is True
    assert data["ready_to_plan"] is True
    assert data["constraints"]["interests"] == []


def test_research_feedback_without_checkpoint_starts_initial_plan(monkeypatch):
    plan_id = _new_plan({"origins": ["上海"], "interests": ["展览"]})
    monkeypatch.setattr(
        app_module,
        "handle_turn",
        lambda *_args, **_kwargs: {
            "plan_id": plan_id,
            "intent": "deep_research",
            "action": "invoke",
            "reply": "继续搜索",
            "constraints_patch": {"__research_feedback": "再搜一批"},
            "booking": None,
            "pending_clarify": [],
        },
    )
    data = client.post(f"/plans/{plan_id}/chat", json={"message": "再搜一批"}).json()
    assert data.get("auto_stream") is not True
    assert data["restart_stream"] is True
    assert "先按现有偏好生成首轮方案" in data["reply"]


def test_repeating_an_identical_refinement_does_not_restart(monkeypatch):
    plan_id = _new_plan({
        "origins": ["上海"],
        "interests": ["展览"],
        "weekend_start": "2026-07-31T00:00:00+08:00",
        "weekend_end": "2026-08-02T23:59:59+08:00",
    })
    with get_session() as session:
        old_thread = session.get(app_module.Plan, int(plan_id)).thread_id

    monkeypatch.setattr(
        app_module,
        "handle_turn",
        lambda *_args, **_kwargs: {
            "plan_id": plan_id,
            "intent": "refine_field",
            "action": "update_state",
            "reply": "已更新",
            "constraints_patch": {"interests": ["展览"]},
            "booking": None,
            "pending_clarify": [],
        },
    )
    data = client.post(f"/plans/{plan_id}/chat", json={"message": "还是看展览"}).json()
    assert data.get("restart_stream") is not True
    assert data["constraints_patch"] is None
    assert "无需重复重算" in data["reply"]
    with get_session() as session:
        assert session.get(app_module.Plan, int(plan_id)).thread_id == old_thread


def test_plan_lock_still_serializes_when_redis_is_down(monkeypatch):
    class DownRedis:
        def ping(self):
            raise OSError("redis unavailable")

    monkeypatch.setattr("wheretogo.infra.redis_client.get_redis", lambda: DownRedis())
    key = f"test-{uuid4()}"
    with plan_lock(key, blocking_timeout=0):
        with pytest.raises(TimeoutError):
            with plan_lock(key, blocking_timeout=0):
                pass


def test_busy_stream_returns_structured_sse_error(monkeypatch):
    @contextmanager
    def busy_lock(*_args, **_kwargs):
        raise TimeoutError("busy")
        yield  # pragma: no cover

    monkeypatch.setattr(app_module, "plan_lock", busy_lock)
    events = list(app_module._run_stream("1", iter(())))
    assert len(events) == 1
    assert events[0]["event"] == "error"
    payload = json.loads(events[0]["data"])
    assert payload["code"] == "PLAN_BUSY"
    assert payload["degraded"] is False


def test_stream_factory_runs_only_after_lock_is_acquired(monkeypatch):
    state = {"inside": False}

    @contextmanager
    def tracked_lock(*_args, **_kwargs):
        state["inside"] = True
        try:
            yield True
        finally:
            state["inside"] = False

    def factory():
        assert state["inside"] is True
        return iter(())

    monkeypatch.setattr(app_module, "plan_lock", tracked_lock)
    assert list(app_module._run_stream("1", factory)) == []


def test_unexpected_stream_failure_is_a_structured_degraded_error(monkeypatch):
    @contextmanager
    def ok_lock(*_args, **_kwargs):
        yield True

    def broken_stream():
        raise TimeoutError("provider timed out")

    monkeypatch.setattr(app_module, "plan_lock", ok_lock)
    events = list(app_module._run_stream("1", broken_stream))
    payload = json.loads(events[0]["data"])
    assert payload["code"] == "STREAM_FAILED"
    assert payload["degraded"] is True
