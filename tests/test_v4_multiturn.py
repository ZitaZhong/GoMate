"""v4 多轮长程对话：目标累积 / 追加 child / 身份变更替换 / 保留重排 / 删除 /
混合轮 / 长程连续 / 运行中直答（技术方案 v4 §10.2 运行中持续对话）。

复杂多变的真实任务：想去 A → 又想加 B → 想吃餐厅 → 中途变更目的地/删除某项。
Worker 不起独立进程：直接同步驱动 commit_turn / poll_once / execute_run。
所有解释器输出用脚本化 TurnDecision 模拟（离线确定性），断言确定性运行时的分派与持久化。
"""
from __future__ import annotations

import uuid

import pytest

from wheretogo.agent.status import RunStatus, TurnStatus
from wheretogo.agent.transaction import commit_turn
from wheretogo.agent.worker import poll_once
from wheretogo.copilot.turn_schema import TurnDecision
from wheretogo.models import AgentOutbox, AgentRun, AgentTurn, Plan, TripBundle

pytestmark = pytest.mark.usefixtures("session")


@pytest.fixture(autouse=True)
def _isolate(session):
    """共库隔离：屏蔽库内遗留 pending outbox，避免 poll_once 领到其它会话任务。"""
    session.query(AgentOutbox).filter(AgentOutbox.status == "pending").update(
        {AgentOutbox.status: "done"}, synchronize_session=False
    )
    session.flush()
    yield


def _subgoal(obj: str) -> dict:
    return {"id": f"g_{obj}", "objective": obj, "acceptance_criteria": [obj],
            "required": True, "target_count": 1}


def _research_decision(subgoals, *, acts=None, patch_extra=None) -> TurnDecision:
    patch = {"target_city_name": "上海", "research_subgoals": subgoals}
    patch.update(patch_extra or {})
    return TurnDecision(
        primary_intent="provide_constraints",
        acts=acts or ["update_constraints", "research_more"],
        constraints_patch=patch,
        goals=[{"id": s["id"], "objective": s["objective"], "required": True} for s in subgoals],
        proposed_actions=[{"type": "research", "reason": "调研"}],
        interpretation_source="rules",
    )


class ScriptedInterpreter:
    """按调用顺序返回预设 TurnDecision，并记录每轮收到的 conversation 长度。"""

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.i = 0
        self.seen_conversation_lens: list[int] = []

    def __call__(self, message, **kwargs):
        self.seen_conversation_lens.append(len(kwargs.get("conversation") or []))
        d = self.decisions[min(self.i, len(self.decisions) - 1)]
        self.i += 1
        return d


class FakePlanner:
    def __init__(self, response="已为你核实并生成一天路线。", activities=None):
        self._resp = response
        self._acts = activities if activities is not None else [{"title": "豫园"}]
        self.calls: list[str] = []

    def _stream(self):
        yield {"event": "progress", "node": "research",
               "data": {"message": "研究中"}}
        yield {"event": "interrupt", "node": "await_booking",
               "data": {"explore_bundle": {
                   "assistant_response": self._resp,
                   "activities": self._acts,
                   "itinerary_draft": [],
                   "research_context": {"summary": "已完成"},
               }}}

    def stream_start(self, plan_id, constraints, conversation=None, thread_id=None):
        self.calls.append("stream_start")
        yield from self._stream()

    def stream_research_more(self, plan_id, thread_id=None):
        self.calls.append("stream_research_more")
        yield from self._stream()

    def prepare_research_more(self, *a, **k):
        self.calls.append("prepare_research_more")

    def get_state(self, plan_id, thread_id=None):
        from types import SimpleNamespace
        return SimpleNamespace(values={"constraints": {"target_city_name": "上海"}})


def _uuid(v):
    return uuid.UUID(str(v))


# ---------- 1. 目标跨轮累积（追加语义） ----------

def test_subgoals_accumulate_across_turns(session, monkeypatch):
    """想去豫园 → 又想加外滩：第二轮解释器输出累积集，约束持久化两者。"""
    script = ScriptedInterpreter([
        _research_decision([_subgoal("豫园")]),
        _research_decision([_subgoal("豫园"), _subgoal("外滩")]),
    ])
    monkeypatch.setattr("wheretogo.agent.transaction.interpret_turn", script)
    planner = FakePlanner()

    t1 = commit_turn("new", "上海想去豫园", session=session)
    plan_id = t1["plan_id"]
    assert poll_once(session=session, planner=planner) == RunStatus.SUCCEEDED.value

    t2 = commit_turn(plan_id, "还想去外滩", session=session)
    # 约束里累积了两个子目标
    plan = session.get(Plan, int(plan_id))
    objectives = [s["objective"] for s in plan.constraints.get("research_subgoals", [])]
    assert objectives == ["豫园", "外滩"]
    # 已有研究结果 + research_more → 续研模式，不是全量重启
    run2 = session.get(AgentRun, _uuid(t2["run"]["id"]))
    assert run2.execution_plan.get("mode") == "research_more"
    assert run2.parent_run_id is None  # 前轮已完成，非 child


# ---------- 2. 运行中追加 → child run ----------

def test_append_during_active_run_creates_child(session, monkeypatch):
    """第一轮研究仍在排队时追加需求 → child run，父任务不取消。"""
    script = ScriptedInterpreter([
        _research_decision([_subgoal("豫园")]),
        _research_decision([_subgoal("豫园"), _subgoal("外滩")]),
    ])
    monkeypatch.setattr("wheretogo.agent.transaction.interpret_turn", script)

    t1 = commit_turn("new", "上海想去豫园", session=session)  # 不执行，保持 queued
    t2 = commit_turn(t1["plan_id"], "还想去外滩", session=session)
    assert t2["run"]["parent_run_id"] == t1["run"]["id"]  # child
    parent = session.get(AgentRun, _uuid(t1["run"]["id"]))
    assert parent.cancel_requested is False  # 追加不取消父任务


# ---------- 3. 中途变更目的地 → 替换 run ----------

def test_destination_change_replaces_active_run(session, monkeypatch):
    """想去上海 → 改成杭州：核心检索身份变化，取消当前任务并替换执行。"""
    script = ScriptedInterpreter([
        _research_decision([_subgoal("豫园")]),
        TurnDecision(
            primary_intent="refine_field",
            acts=["update_constraints", "research_more"],
            constraints_patch={"target_city_name": "杭州", "research_subgoals": [_subgoal("西湖")]},
            goals=[{"id": "g_西湖", "objective": "西湖", "required": True}],
            proposed_actions=[{"type": "research", "reason": "换城重研"}],
            interpretation_source="rules",
        ),
    ])
    monkeypatch.setattr("wheretogo.agent.transaction.interpret_turn", script)

    t1 = commit_turn("new", "上海想去豫园", session=session)  # queued, active
    plan = session.get(Plan, int(t1["plan_id"]))
    old_thread = plan.thread_id
    t2 = commit_turn(t1["plan_id"], "算了改成杭州", session=session)

    active = session.get(AgentRun, _uuid(t1["run"]["id"]))
    assert active.cancel_requested is True  # 旧任务被取消
    run2 = session.get(AgentRun, _uuid(t2["run"]["id"]))
    assert run2.parent_run_id is None  # 替换而非 child
    assert run2.execution_plan.get("replaces_run_id") == t1["run"]["id"]
    session.refresh(plan)
    assert plan.thread_id != old_thread  # 新线程重跑 discovery
    assert plan.constraints["target_city_name"] == "杭州"


# ---------- 4. 禁止搜索的保留重排（引用之前项 + 保留 + 新增位） ----------

def test_no_search_recompose_preserves_and_is_self_contained(session, monkeypatch):
    """保留前面的，把下午换成已有候选：recompose，不搜索，回复自洽。"""
    plan = Plan(stage="await_booking", thread_id=f"pending-{uuid.uuid4()}",
                constraints={"target_city_name": "上海"})
    session.add(plan)
    session.flush()
    plan.thread_id = f"plan:{plan.id}"
    session.add(TripBundle(plan_id=plan.id, version="explore", payload={
        "assistant_response": "已有方案。",
        "activities": [{"title": "豫园", "venue": "黄浦区"}, {"title": "外滩", "venue": "中山东一路"}],
        "itinerary_draft": [
            {"day": "周六", "time_window": "上午", "candidate_title": "豫园", "reason": "先游园"},
        ],
    }))
    session.flush()
    script = ScriptedInterpreter([
        TurnDecision(
            primary_intent="ask_info",
            acts=["recompose_plan"],
            goals=[{"id": "g1", "objective": "重排：保留豫园，下午加外滩", "required": True}],
            proposed_actions=[{"type": "compose_itinerary", "reason": "本地重排"}],
            itinerary_draft=[
                {"day": "周六", "time_window": "上午", "candidate_title": "豫园", "reason": "先游园"},
                {"day": "周六", "time_window": "下午", "candidate_title": "外滩", "reason": "傍晚看江景"},
            ],
            interpretation_source="rules",
        ),
    ])
    monkeypatch.setattr("wheretogo.agent.transaction.interpret_turn", script)

    out = commit_turn(str(plan.id), "不要搜索，保留豫园，下午加外滩", session=session)
    assert out["run"]["type"] == "recompose"
    assert poll_once(session=session, planner=FakePlanner()) == RunStatus.SUCCEEDED.value
    turn = session.get(AgentTurn, _uuid(out["turn_id"]))
    reply = turn.visible_reply
    assert "豫园" in reply and "外滩" in reply  # 保留 + 新增位都在
    assert "未做外部搜索" in reply
    for pointer in ("见下方", "见卡片"):
        assert pointer not in reply


# ---------- 5. 删除某项（收缩需求） ----------

def test_remove_item_updates_constraints(session, monkeypatch):
    """先要豫园+外滩，再说不要外滩了：约束收缩为只剩豫园。"""
    script = ScriptedInterpreter([
        _research_decision([_subgoal("豫园"), _subgoal("外滩")]),
        _research_decision([_subgoal("豫园")], patch_extra={
            "experience_requirements": ["豫园"],
        }),
    ])
    monkeypatch.setattr("wheretogo.agent.transaction.interpret_turn", script)
    planner = FakePlanner()

    t1 = commit_turn("new", "上海想去豫园和外滩", session=session)
    assert poll_once(session=session, planner=planner) == RunStatus.SUCCEEDED.value
    commit_turn(t1["plan_id"], "不要外滩了", session=session)

    plan = session.get(Plan, int(t1["plan_id"]))
    objectives = [s["objective"] for s in plan.constraints.get("research_subgoals", [])]
    assert objectives == ["豫园"]  # 外滩已移除


# ---------- 6. 混合轮：一句话同时改约束 + 追加研究 ----------

def test_mixed_turn_updates_constraints_and_creates_run(session, monkeypatch):
    """改预算 + 再加个展览：约束更新与研究任务同时发生。"""
    script = ScriptedInterpreter([
        _research_decision([_subgoal("豫园")]),
        _research_decision(
            [_subgoal("豫园"), _subgoal("展览")],
            patch_extra={"budget_band": {"max": 500}},
        ),
    ])
    monkeypatch.setattr("wheretogo.agent.transaction.interpret_turn", script)
    planner = FakePlanner()

    t1 = commit_turn("new", "上海想去豫园", session=session)
    assert poll_once(session=session, planner=planner) == RunStatus.SUCCEEDED.value
    t2 = commit_turn(t1["plan_id"], "预算改到500，再加个展览", session=session)

    plan = session.get(Plan, int(t1["plan_id"]))
    assert plan.constraints["budget_band"] == {"max": 500}  # 约束更新
    assert t2["run"] and t2["run"]["id"]  # 同时创建了研究任务
    objectives = [s["objective"] for s in plan.constraints.get("research_subgoals", [])]
    assert "展览" in objectives


# ---------- 7. 长程连续：多轮累积 + 解释器每轮看到增长的历史 ----------

def test_long_horizon_conversation_grows_and_interpreter_sees_history(session, monkeypatch):
    script = ScriptedInterpreter([
        _research_decision([_subgoal("豫园")]),
        _research_decision([_subgoal("豫园"), _subgoal("外滩")]),
        _research_decision([_subgoal("豫园"), _subgoal("外滩"), _subgoal("餐厅")]),
        _research_decision([_subgoal("豫园"), _subgoal("外滩"), _subgoal("餐厅"), _subgoal("展览")]),
    ])
    monkeypatch.setattr("wheretogo.agent.transaction.interpret_turn", script)
    planner = FakePlanner()

    plan_id = None
    messages = ["上海想去豫园", "还想去外滩", "中午想找家餐厅", "再加个展览"]
    for i, msg in enumerate(messages):
        out = commit_turn(plan_id or "new", msg, session=session)
        plan_id = out["plan_id"]
        poll_once(session=session, planner=planner)

    plan = session.get(Plan, int(plan_id))
    # 会话逐轮累积（每轮 user+assistant ≥ 2 条，含 research_result 轮）
    assert len(plan.conversation) >= len(messages) * 2
    # Turn 序号递增
    turns = (
        session.query(AgentTurn)
        .filter_by(plan_id=int(plan_id))
        .order_by(AgentTurn.sequence_no)
        .all()
    )
    assert [t.sequence_no for t in turns] == [1, 2, 3, 4]
    # 解释器每轮收到的历史严格增长（长程记忆连续）
    lens = script.seen_conversation_lens
    assert lens == sorted(lens) and lens[-1] > lens[0]
    # 最终累积四个子目标
    objectives = [s["objective"] for s in plan.constraints.get("research_subgoals", [])]
    assert objectives == ["豫园", "外滩", "餐厅", "展览"]


# ---------- 8. 运行中纯问答：直接回答，不打扰在跑的任务 ----------

def test_pure_question_during_active_run_answers_without_new_run(session, monkeypatch):
    """研究在跑时用户问一句"外滩要门票吗"：直接回答，不新建 run，活跃任务不受扰。"""
    script = ScriptedInterpreter([
        _research_decision([_subgoal("豫园")]),
        TurnDecision(
            primary_intent="ask_info",
            acts=["answer_info"],
            assistant_reply="外滩是开放式景区，不需要门票。",
            proposed_actions=[{"type": "answer", "reason": "回答"}],
            interpretation_source="rules",
        ),
    ])
    monkeypatch.setattr("wheretogo.agent.transaction.interpret_turn", script)

    t1 = commit_turn("new", "上海想去豫园", session=session)  # queued, active
    t2 = commit_turn(t1["plan_id"], "外滩要门票吗", session=session)

    assert t2["turn_status"] == TurnStatus.ANSWERED.value
    assert t2["run"] is None  # 纯问答不新建任务
    assert "门票" in t2["assistant_message"]["content"]
    parent = session.get(AgentRun, _uuid(t1["run"]["id"]))
    assert parent.cancel_requested is False  # 在跑的研究任务不受打扰
    assert RunStatus(parent.status) in {RunStatus.QUEUED, RunStatus.RUNNING}
