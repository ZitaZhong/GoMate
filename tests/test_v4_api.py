"""v4 Agent API 端到端场景（技术方案 v4 §9.1 / §11.3 / §17.3）。

场景 A：市内路线不要求出发地立即 running；场景 B：跨城比较阻塞提问且回答后继续；
场景 C：运行中追加 → child run；场景 D：禁止搜索 → recompose 不建 research run。
"""
from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient

from wheretogo.agent.status import RunStatus, TurnStatus
from wheretogo.agent.worker import poll_once
from wheretogo.bff.app import app
from wheretogo.copilot.turn_schema import TurnDecision
from wheretogo.db import get_session
from wheretogo.models import AgentRun, AgentTurn, Plan, TripBundle

client = TestClient(app)


def _decision(**overrides) -> TurnDecision:
    values = dict(
        primary_intent="provide_constraints",
        acts=["update_constraints", "research_more"],
        constraints_patch={},
        goals=[],
        proposed_actions=[],
        assistant_reply=None,
        interpretation_source="rules",
    )
    values.update(overrides)
    return TurnDecision(**values)


def _patch_interpreter(monkeypatch, decision: TurnDecision):
    monkeypatch.setattr(
        "wheretogo.agent.transaction.interpret_turn",
        lambda *args, **kwargs: decision,
    )


class FakePlanner:
    def __init__(self):
        self.calls: list[str] = []

    def stream_start(self, plan_id, constraints, conversation=None, thread_id=None):
        self.calls.append("stream_start")
        yield {"event": "progress", "node": "research",
               "data": {"message": "已完成 1/1 个研究任务"}}
        yield {"event": "interrupt", "node": "await_booking",
               "data": {"explore_bundle": {
                   "assistant_response": "已生成包含四个地点的一日路线。",
                   "activities": [
                       {"title": "上海博物馆"}, {"title": "世博馆"},
                       {"title": "外滩"}, {"title": "新天地"},
                   ],
               }}}

    def stream_research_more(self, plan_id, thread_id=None):
        self.calls.append("stream_research_more")
        yield from self.stream_start(plan_id, {}, thread_id=thread_id)

    def stream_replan(self, plan_id, reason, from_node, thread_id=None):
        yield from self.stream_start(plan_id, {}, thread_id=thread_id)

    def prepare_research_more(self, *args, **kwargs):
        self.calls.append("prepare_research_more")

    def get_state(self, plan_id, thread_id=None):
        from types import SimpleNamespace
        return SimpleNamespace(values={})


def _drain(max_items: int = 50) -> None:
    """清空遗留 pending 任务（共享 dev 库：其它用例/历史会话的 Run 不应干扰断言）。"""
    for _ in range(max_items):
        if poll_once(planner=FakePlanner()) is None:
            return


@pytest.fixture(autouse=True)
def clean_outbox():
    _drain()
    yield
    # 用例自身创建的 run 也要消化掉，不给后续用例/真实 worker 泄漏 pending 任务
    _drain()


# ---------- 场景 A：本次真实故障（Plan 4491 等价） ----------

def test_scenario_a_in_city_route_starts_run_without_origin(monkeypatch):
    _patch_interpreter(monkeypatch, _decision(
        constraints_patch={"target_city_name": "上海"},
        goals=[{"id": "g1", "objective": "把四个指定地点安排成一天内顺路的路线",
                "required": True}],
        proposed_actions=[{"type": "research", "reason": "核实开放时间与位置"}],
        clarification_candidates=[{"fact": "start_location", "reason": "优化首段交通"}],
    ))
    started = time.monotonic()
    r = client.post(
        "/agent/conversations/new/turns",
        json={"message": "我要去上海博物馆、世博馆、外滩和新天地，帮我设计一条一天的路线"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    elapsed = time.monotonic() - started
    assert r.status_code == 202  # 运行中响应
    d = r.json()
    assert elapsed < 5  # 立即返回，不做同步研究
    assert d["turn_status"] == TurnStatus.RUNNING.value
    assert d["run"] and d["run"]["id"]  # 不要求先提供跨城出发地
    assert d["run"]["events_url"].startswith("/agent/runs/")
    # 非阻塞提示（可选补充酒店/车站），不阻塞任务
    if d.get("clarification"):
        assert d["clarification"]["blocking"] is False
    # 真实 Run 已持久化
    with get_session() as s:
        run = s.get(AgentRun, uuid.UUID(d["run"]["id"]))
        assert run is not None and run.status == RunStatus.QUEUED.value


def test_idempotency_key_replays_same_turn(monkeypatch):
    _patch_interpreter(monkeypatch, _decision(
        constraints_patch={"target_city_name": "上海"},
        goals=[{"id": "g1", "objective": "市内一日路线", "required": True}],
        proposed_actions=[{"type": "research", "reason": "核实"}],
    ))
    key = str(uuid.uuid4())
    first = client.post("/agent/conversations/new/turns",
                        json={"message": "上海一日路线"},
                        headers={"Idempotency-Key": key}).json()
    again = client.post(f"/agent/conversations/{first['plan_id']}/turns",
                        json={"message": "上海一日路线"},
                        headers={"Idempotency-Key": key}).json()
    assert again["idempotent"] is True
    assert again["turn_id"] == first["turn_id"]
    assert again["run"]["id"] == first["run"]["id"]


# ---------- 场景 B：真正需要出发地 ----------

def test_scenario_b_intercity_blocks_then_continues(monkeypatch):
    _patch_interpreter(monkeypatch, _decision(
        constraints_patch={"target_city_name": "上海"},
        goals=[{"id": "g1", "objective": "比较去上海坐高铁还是飞机", "required": True}],
        proposed_actions=[{"type": "transport_search", "reason": "跨城交通比较"}],
    ))
    r = client.post("/agent/conversations/new/turns",
                    json={"message": "帮我比较去上海坐高铁还是飞机"})
    assert r.status_code == 200
    d = r.json()
    plan_id = d["plan_id"]
    assert d["turn_status"] == TurnStatus.NEEDS_INPUT.value
    assert d["run"] is None  # 不启动无意义交通查询
    assert d["clarification"]["blocking"] is True
    assert "出发" in d["clarification"]["question"]  # 问题在界面可见
    # 阻塞澄清必须出现在 Workspace API（刷新后仍显示）
    ws = client.get(f"/agent/conversations/{plan_id}/workspace").json()
    assert any(c["blocking"] for c in ws["open_clarifications"])
    assert ws["active_turn"]["status"] == TurnStatus.NEEDS_INPUT.value

    # 用户回答出发地 → 从原上下文继续，创建真实 Run
    _patch_interpreter(monkeypatch, _decision(
        constraints_patch={"origins": ["杭州"]},
        goals=[{"id": "g1", "objective": "比较去上海坐高铁还是飞机", "required": True}],
        proposed_actions=[{"type": "transport_search", "reason": "跨城交通比较"}],
    ))
    r2 = client.post(f"/agent/conversations/{plan_id}/turns",
                     json={"message": "从杭州出发"})
    d2 = r2.json()
    assert d2["turn_status"] == TurnStatus.RUNNING.value
    assert d2["run"] and d2["run"]["id"]
    # 澄清已闭环：Workspace 不再有 open 阻塞澄清
    ws2 = client.get(f"/agent/conversations/{plan_id}/workspace").json()
    assert not ws2["open_clarifications"]


# ---------- 场景 D：明确禁止搜索 ----------

def test_scenario_d_no_search_creates_recompose_run(monkeypatch):
    # 预置已有研究结果（explore bundle）
    with get_session() as s:
        p = Plan(stage="await_booking", thread_id=f"pending-{uuid.uuid4()}", constraints={
            "target_city_name": "上海",
        })
        s.add(p)
        s.flush()
        p.thread_id = f"plan:{p.id}"
        s.add(TripBundle(plan_id=p.id, version="explore", payload={
            "assistant_response": "已有方案。",
            "activities": [{"title": "上海博物馆"}, {"title": "外滩"}],
            "itinerary_draft": [
                {"day": "周六", "time_window": "上午", "candidate_title": "上海博物馆",
                 "reason": "先看展"},
            ],
        }))
        plan_id = str(p.id)
    _patch_interpreter(monkeypatch, _decision(
        primary_intent="ask_info",
        acts=["recompose_plan"],
        goals=[{"id": "g1", "objective": "只基于现有候选重排", "required": True}],
        proposed_actions=[{"type": "compose_itinerary", "reason": "本地重排"}],
        itinerary_draft=[
            {"day": "周六", "time_window": "下午", "candidate_title": "外滩",
             "reason": "傍晚看江景"},
        ],
    ))
    r = client.post(f"/agent/conversations/{plan_id}/turns",
                    json={"message": "不要再搜索，只基于现有候选重排"})
    d = r.json()
    assert d["turn_status"] == TurnStatus.RUNNING.value
    assert d["run"]["type"] == "recompose"  # 不创建 Research Run
    # 执行：只使用当前 Workspace，不调用外部搜索
    planner = FakePlanner()
    status = poll_once(planner=planner)
    assert status == RunStatus.SUCCEEDED.value
    assert planner.calls == []  # 未进研究图
    with get_session() as s:
        run = s.get(AgentRun, uuid.UUID(d["run"]["id"]))
        turn = s.get(AgentTurn, uuid.UUID(d["turn_id"]))
        assert run.status == RunStatus.SUCCEEDED.value
        assert turn.status == TurnStatus.ANSWERED.value
        assert "未做外部搜索" in turn.visible_reply
        # 结果持久化：新的 explore bundle
        bundles = s.query(TripBundle).filter_by(
            plan_id=int(plan_id), version="explore"
        ).count()
        assert bundles >= 2


# ---------- 场景 C：运行中追加需求 → child run ----------

def test_scenario_c_addition_during_run_creates_child_run(monkeypatch):
    _patch_interpreter(monkeypatch, _decision(
        constraints_patch={"target_city_name": "上海"},
        goals=[{"id": "g1", "objective": "市内四地点一日路线", "required": True}],
        proposed_actions=[{"type": "research", "reason": "核实"}],
    ))
    first = client.post("/agent/conversations/new/turns",
                        json={"message": "上海四地点一日路线"}).json()
    plan_id = first["plan_id"]
    parent_run_id = first["run"]["id"]
    # 父 Run 仍 queued（未执行），用户追加子目标
    _patch_interpreter(monkeypatch, _decision(
        constraints_patch={"experience_requirements": ["加一家本帮菜餐馆"]},
        goals=[
            {"id": "g1", "objective": "保留四个地点", "required": True},
            {"id": "g2", "objective": "路线里加一家本帮菜餐馆", "required": True},
        ],
        proposed_actions=[{"type": "research", "reason": "补充餐馆研究"}],
    ))
    second = client.post(f"/agent/conversations/{plan_id}/turns",
                         json={"message": "路线里再加入一家本帮菜餐馆，前面四个地点保留"}).json()
    assert second["turn_status"] == TurnStatus.RUNNING.value
    assert second["run"]["parent_run_id"] == parent_run_id  # child run
    child_run_id = second["run"]["id"]
    # Worker：child 等父终态才领取：第一次轮询只执行父 Run
    planner = FakePlanner()
    first_status = poll_once(planner=planner)
    assert first_status == RunStatus.SUCCEEDED.value
    with get_session() as s:
        parent = s.get(AgentRun, uuid.UUID(parent_run_id))
        child = s.get(AgentRun, uuid.UUID(child_run_id))
        assert parent.status == RunStatus.SUCCEEDED.value
        assert child.status == RunStatus.QUEUED.value  # 仍在等待父终态后领取
    second_status = poll_once(planner=planner)
    assert second_status == RunStatus.SUCCEEDED.value
    with get_session() as s:
        child = s.get(AgentRun, uuid.UUID(child_run_id))
        assert child.status == RunStatus.SUCCEEDED.value


# ---------- Workspace 恢复与事件续传 ----------

def test_workspace_restores_active_run_and_events(monkeypatch):
    _patch_interpreter(monkeypatch, _decision(
        constraints_patch={"target_city_name": "上海"},
        goals=[{"id": "g1", "objective": "市内一日路线", "required": True}],
        proposed_actions=[{"type": "research", "reason": "核实"}],
    ))
    d = client.post("/agent/conversations/new/turns",
                    json={"message": "上海一日路线"}).json()
    plan_id, run_id = d["plan_id"], d["run"]["id"]
    ws = client.get(f"/agent/conversations/{plan_id}/workspace").json()
    assert ws["active_run"]["id"] == run_id  # 刷新后可恢复运行中任务
    assert ws["last_event_id"] >= 1  # queued 事件已可续传
    # 执行完成后：active_run 清空，current_plan 可渲染
    poll_once(planner=FakePlanner())
    ws2 = client.get(f"/agent/conversations/{plan_id}/workspace").json()
    assert ws2["active_run"] is None
    assert ws2["current_plan"]["activities"]
    assert ws2["active_turn"]["status"] == TurnStatus.ANSWERED.value
    # SSE 续传：after=0 能读到终态事件；断线后按 Last-Event-ID 不重复
    with client.stream("GET", f"/agent/runs/{run_id}/events") as resp:
        body = "".join(resp.iter_text())
    assert "run.status" in body and '"final": true' in body.replace('":true', '": true')


def test_legacy_boolean_fields_derived_not_authoritative(monkeypatch):
    _patch_interpreter(monkeypatch, _decision(
        constraints_patch={"target_city_name": "上海"},
        goals=[{"id": "g1", "objective": "市内一日路线", "required": True}],
        proposed_actions=[{"type": "research", "reason": "核实"}],
    ))
    d = client.post("/agent/conversations/new/turns",
                    json={"message": "上海一日路线"}).json()
    # 首轮 research 走全量启动：restart_stream 由新状态派生
    assert d["ready_to_plan"] is True
    assert d["restart_stream"] is True
    assert d["auto_stream"] is False
    # 纯回答回合：三个布尔全 false
    _patch_interpreter(monkeypatch, _decision(
        primary_intent="ask_info",
        acts=["answer_info"],
        assistant_reply="外滩不需要门票。",
        proposed_actions=[{"type": "answer", "reason": "回答"}],
    ))
    d2 = client.post(f"/agent/conversations/{d['plan_id']}/turns",
                     json={"message": "外滩要门票吗"}).json()
    assert d2["turn_status"] == TurnStatus.ANSWERED.value
    assert d2["auto_stream"] is False and d2["restart_stream"] is False


def test_metrics_report_zero_silent_failures():
    m = client.get("/agent/metrics").json()
    assert m["turn_silent_terminal_total"] == 0
    assert m["promised_without_run_total"] == 0
    assert m["hidden_clarification_total"] == 0


def test_unknown_plan_returns_404():
    r = client.post("/agent/conversations/999999999/turns", json={"message": "hi"})
    assert r.status_code == 404
    r2 = client.get("/agent/conversations/999999999/workspace")
    assert r2.status_code == 404


def test_destination_clarify_loop_broken_by_short_answer(monkeypatch):
    """回归（真实故障）：目的地阻塞追问后，短答案“上海/就是上海市内”必须破循环。"""
    from wheretogo.copilot.interpreter import interpret_turn as real_interpret

    def fake(message, **kwargs):
        if "好玩" in message:
            return _decision(
                goals=[{"id": "g1", "objective": "找个好玩的地方", "required": True}],
                proposed_actions=[{"type": "research", "reason": "需要调研"}],
            )
        kwargs["use_llm"] = False  # 短答案走真实规则解释（离线确定性）
        return real_interpret(message, **kwargs)

    monkeypatch.setattr("wheretogo.agent.transaction.interpret_turn", fake)
    first = client.post("/agent/conversations/new/turns",
                        json={"message": "这周末帮我找个好玩的地方"}).json()
    assert first["turn_status"] == TurnStatus.NEEDS_INPUT.value
    assert "城市" in first["clarification"]["question"]
    # 阻塞回复不得含承诺语言（此时没有真实 Run）
    assert "已创建" not in first["assistant_message"]["content"]
    # 短答案绑定目的地 → 立即转入 running，不再重复追问
    second = client.post(f"/agent/conversations/{first['plan_id']}/turns",
                         json={"message": "就是上海市内，不出城"}).json()
    assert second["turn_status"] == TurnStatus.RUNNING.value
    assert second["run"] and second["run"]["id"]
    ws = client.get(f"/agent/conversations/{first['plan_id']}/workspace").json()
    # 目的地需求已满足：不再有阻塞澄清（可能仍有 start_location 非阻塞提示）
    assert not [c for c in ws["open_clarifications"] if c["blocking"]]


def test_booking_import_accepts_manual_kind(monkeypatch):
    """回归：前端回填面板默认 kind=manual（由文本自动识别），不得再 422。"""
    _patch_interpreter(monkeypatch, _decision(
        constraints_patch={"target_city_name": "上海"},
        goals=[{"id": "g1", "objective": "市内一日路线", "required": True}],
        proposed_actions=[{"type": "research", "reason": "核实"}],
    ))
    d = client.post("/agent/conversations/new/turns",
                    json={"message": "上海一日路线"}).json()
    r = client.post(
        f"/plans/{d['plan_id']}/bookings/import",
        json={"kind": "manual", "input_kind": "text",
              "raw": "G7503次 杭州东到上海虹桥 2026-08-01 08:05开 二等座"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["booking"]["kind"] == "train"  # 文本自动识别为车次
    assert body["booking"]["extracted"].get("train_no") == "G7503"
