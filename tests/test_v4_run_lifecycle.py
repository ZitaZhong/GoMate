"""v4 Run 生命周期：原子提交 / 幂等 / Outbox / 事件续传 / stalled / 取消 / 降级。

Worker 不起独立进程：直接同步驱动 poll_once / execute_run（技术方案 v4 §9 / §13）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wheretogo.agent import events as run_events
from wheretogo.agent.status import ErrorCode, RunStatus, TurnStatus
from wheretogo.agent.supervisor import execute_run
from wheretogo.agent.transaction import commit_turn
from wheretogo.agent.worker import poll_once, scan_stalled
from wheretogo.copilot.turn_schema import TurnDecision
from wheretogo.models import AgentOutbox, AgentRun, AgentTurn, Plan

pytestmark = pytest.mark.usefixtures("session")


@pytest.fixture(autouse=True)
def isolate_shared_outbox(session):
    """共库隔离：测试与真实服务/worker 共用 dev 库，屏蔽库内已有 pending 行，
    避免 poll_once 领到其它会话的任务；并把历史活跃 Run 的心跳置为当前，
    使 scan_stalled 不会误伤 E2E 残留的遗留 Run；SAVEPOINT 回滚后不留痕。"""
    from datetime import datetime as _dt, timezone as _tz

    session.query(AgentOutbox).filter(AgentOutbox.status == "pending").update(
        {AgentOutbox.status: "done"}, synchronize_session=False
    )
    session.query(AgentRun).filter(
        AgentRun.status.in_(["queued", "running", "waiting_tool", "composing"])
    ).update(
        {AgentRun.heartbeat_at: _dt.now(_tz.utc)}, synchronize_session=False
    )
    session.flush()
    yield


def _research_decision(**overrides) -> TurnDecision:
    values = dict(
        primary_intent="provide_constraints",
        acts=["update_constraints", "research_more"],
        constraints_patch={"target_city_name": "上海"},
        goals=[{"id": "g1", "objective": "核实四个地点并生成市内路线", "required": True}],
        proposed_actions=[{"type": "research", "reason": "需要核实开放时间"}],
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
    """轻量 planner：产出可配置事件流，模拟 LangGraph 执行。"""

    def __init__(self, events=None):
        self.events = events if events is not None else [
            {"event": "progress", "node": "research",
             "data": {"message": "已完成 1/2 个研究任务", "completed": 1, "total": 2}},
            {"event": "interrupt", "node": "await_booking",
             "data": {"explore_bundle": {
                 "assistant_response": "已为你核实四个地点并生成一天路线。",
                 "activities": [{"title": "上海博物馆"}],
                 "itinerary_draft": [],
                 "research_context": {"summary": "四地点核实完成"},
             }}},
        ]
        self.calls: list[str] = []

    def stream_start(self, plan_id, constraints, conversation=None, thread_id=None):
        self.calls.append("stream_start")
        yield from self.events

    def stream_research_more(self, plan_id, thread_id=None):
        self.calls.append("stream_research_more")
        yield from self.events

    def stream_replan(self, plan_id, reason, from_node, thread_id=None):
        self.calls.append("stream_replan")
        yield from self.events

    def prepare_research_more(self, *args, **kwargs):
        self.calls.append("prepare_research_more")

    def get_state(self, plan_id, thread_id=None):
        from types import SimpleNamespace
        return SimpleNamespace(values={})


# ---------- 原子提交与幂等（§9.2 / §9.3） ----------

def test_commit_turn_atomically_creates_turn_run_outbox(session, monkeypatch):
    _patch_interpreter(monkeypatch, _research_decision())
    out = commit_turn("new", "帮我设计上海一日路线", client_key="key-1", session=session)
    assert out["turn_status"] == "running"
    assert out["run"]["id"]
    turn = session.get(AgentTurn, __import__("uuid").UUID(out["turn_id"]))
    run = session.get(AgentRun, __import__("uuid").UUID(out["run"]["id"]))
    assert turn.status == TurnStatus.RUNNING.value
    assert str(turn.run_id) == out["run"]["id"]
    assert run.status == RunStatus.QUEUED.value
    assert run.checkpoint_ref  # Run 绑定 checkpoint 线程
    outbox = session.query(AgentOutbox).filter(
        AgentOutbox.payload["run_id"].astext == out["run"]["id"]
    ).all()
    assert len(outbox) == 1 and outbox[0].status == "pending"
    # 运行中回复由模板生成且承诺有 Run 支撑
    assert "已创建" in out["assistant_message"]["content"]


def test_commit_turn_idempotent_same_key_reuses_turn_and_run(session, monkeypatch):
    _patch_interpreter(monkeypatch, _research_decision())
    first = commit_turn("new", "上海一日路线", client_key="idem-1", session=session)
    again = commit_turn(first["plan_id"], "上海一日路线", client_key="idem-1", session=session)
    assert again["idempotent"] is True
    assert again["turn_id"] == first["turn_id"]
    assert again["run"]["id"] == first["run"]["id"]
    # 不产生第二个 Run / Outbox
    runs = session.query(AgentRun).filter_by(plan_id=int(first["plan_id"])).all()
    assert len(runs) == 1


def test_run_creation_failure_yields_honest_failed_turn(session, monkeypatch):
    _patch_interpreter(monkeypatch, _research_decision())
    import wheretogo.agent.events as ev_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(ev_mod, "publish", _boom)
    out = commit_turn("new", "上海一日路线", session=session)
    assert out["turn_status"] == "failed"
    assert out["run"] is None
    assert out["error"]["code"] == ErrorCode.RUN_CREATION_FAILED.value
    # 不允许输出未来时承诺
    assert "已创建" not in out["assistant_message"]["content"]
    assert out["assistant_message"]["content"]  # 诚实错误信息非空


# ---------- Outbox 领取执行与事件流（§6.5 / §9.2） ----------

def test_poll_once_claims_and_executes_run(session, monkeypatch):
    _patch_interpreter(monkeypatch, _research_decision())
    out = commit_turn("new", "上海一日路线", session=session)
    planner = FakePlanner()
    status = poll_once(session=session, planner=planner)
    assert status == RunStatus.SUCCEEDED.value
    assert planner.calls  # 真实执行发生
    run = session.get(AgentRun, __import__("uuid").UUID(out["run"]["id"]))
    turn = session.get(AgentTurn, __import__("uuid").UUID(out["turn_id"]))
    assert run.status == RunStatus.SUCCEEDED.value
    assert turn.status == TurnStatus.ANSWERED.value
    assert "核实四个地点" in turn.visible_reply
    outbox = session.query(AgentOutbox).filter(
        AgentOutbox.payload["run_id"].astext == out["run"]["id"]
    ).one()
    assert outbox.status == "done"
    # 会话中出现 research_result 轮（旧双呈现兼容）
    plan = session.get(Plan, run.plan_id)
    assert any(t.get("intent") == "research_result" for t in plan.conversation)


def test_run_events_monotonic_and_resumable(session, monkeypatch):
    _patch_interpreter(monkeypatch, _research_decision())
    out = commit_turn("new", "上海一日路线", session=session)
    poll_once(session=session, planner=FakePlanner())
    run_id = out["run"]["id"]
    rows = run_events.read_since(session, run_id, 0)
    seqs = [row.sequence for row in rows]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))  # 单调且不重复
    assert seqs[0] == 1
    # Last-Event-ID 续传：从中间序号读，不丢后续事件
    middle = seqs[len(seqs) // 2]
    tail = run_events.read_since(session, run_id, middle)
    assert [row.sequence for row in tail] == [s for s in seqs if s > middle]
    # 终态事件存在（SSE 依此关闭）
    assert any((row.payload or {}).get("final") for row in rows)


def test_composer_failure_degrades_to_partial(session, monkeypatch):
    """研究成功但回复合成失败：成果不丢，Turn=partial（§13.4）。"""
    _patch_interpreter(monkeypatch, _research_decision())
    out = commit_turn("new", "上海一日路线", session=session)
    planner = FakePlanner(events=[
        {"event": "interrupt", "node": "await_booking",
         "data": {"explore_bundle": {
             "assistant_response": "",  # Composer 失败：无最终回复
             "activities": [{"title": "上海博物馆"}, {"title": "外滩"}],
         }}},
    ])
    status = poll_once(session=session, planner=planner)
    assert status == RunStatus.PARTIAL.value
    turn = session.get(AgentTurn, __import__("uuid").UUID(out["turn_id"]))
    assert turn.status == TurnStatus.PARTIAL.value
    # 降级回复仍自洽：内联已得候选名，不再把细节推给卡片
    assert "2 个" in turn.visible_reply and "上海博物馆" in turn.visible_reply
    assert "见卡片" not in turn.visible_reply
    run = session.get(AgentRun, __import__("uuid").UUID(out["run"]["id"]))
    assert run.error_code == ErrorCode.COMPOSITION_FAILED.value


def test_stream_error_without_results_fails_honestly(session, monkeypatch):
    _patch_interpreter(monkeypatch, _research_decision())
    out = commit_turn("new", "上海一日路线", session=session)
    planner = FakePlanner(events=[
        {"event": "error", "node": "graph",
         "data": {"message": "provider timeout", "degraded": True}},
    ])
    status = poll_once(session=session, planner=planner)
    assert status == RunStatus.FAILED.value
    turn = session.get(AgentTurn, __import__("uuid").UUID(out["turn_id"]))
    assert turn.status == TurnStatus.FAILED.value
    assert turn.visible_reply  # 用户可见错误，不静默


# ---------- 取消与 stalled（§13.3） ----------

def test_cancel_requested_before_execution(session, monkeypatch):
    _patch_interpreter(monkeypatch, _research_decision())
    out = commit_turn("new", "上海一日路线", session=session)
    run = session.get(AgentRun, __import__("uuid").UUID(out["run"]["id"]))
    run.cancel_requested = True
    session.flush()
    status = execute_run(run.id, session=session, planner=FakePlanner())
    assert status == RunStatus.CANCELLED.value
    turn = session.get(AgentTurn, __import__("uuid").UUID(out["turn_id"]))
    assert turn.status == TurnStatus.CANCELLED.value


def test_stalled_run_requeued_then_failed(session, monkeypatch):
    from wheretogo.config import get_settings
    _patch_interpreter(monkeypatch, _research_decision())
    out = commit_turn("new", "上海一日路线", session=session)
    run = session.get(AgentRun, __import__("uuid").UUID(out["run"]["id"]))
    stale = datetime.now(timezone.utc) - timedelta(
        seconds=get_settings().agent_run_stall_threshold_s + 60
    )
    run.status = RunStatus.RUNNING.value
    run.heartbeat_at = stale
    session.flush()
    # 第一次：retry_count 未超限 → 重新排队并补发 Outbox（只验证本用例的 run）
    scan_stalled(session=session)
    session.expire_all()
    run = session.get(AgentRun, run.id)
    assert run.status == RunStatus.QUEUED.value
    assert run.retry_count == 1
    # 超过重试上限 → 标记失败并给用户可读错误
    run.status = RunStatus.RUNNING.value
    run.heartbeat_at = stale
    run.retry_count = get_settings().agent_run_max_retries
    session.flush()
    scan_stalled(session=session)
    session.expire_all()
    run = session.get(AgentRun, run.id)
    assert run.status == RunStatus.FAILED.value
    turn = session.get(AgentTurn, run.turn_id)
    assert turn.status == TurnStatus.FAILED.value
    assert turn.error_code == ErrorCode.RUN_STALLED.value


# ---------- 已执行 Run 的幂等保护 ----------

def test_execute_run_is_idempotent_for_non_queued(session, monkeypatch):
    _patch_interpreter(monkeypatch, _research_decision())
    out = commit_turn("new", "上海一日路线", session=session)
    planner = FakePlanner()
    first = poll_once(session=session, planner=planner)
    assert first == RunStatus.SUCCEEDED.value
    # 重复执行（模拟 Outbox 消费重复）：不重跑、状态不变
    again = execute_run(out["run"]["id"], session=session, planner=planner)
    assert again == RunStatus.SUCCEEDED.value
    assert planner.calls.count("stream_start") == 1
