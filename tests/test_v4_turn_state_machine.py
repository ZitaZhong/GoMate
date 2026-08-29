"""v4 Turn 状态机与合约不变量（技术方案 v4 §6.1 / §8.3 / §17.1）。

任何状态都不能同时缺少 answer + question + run + error；
RUNNING 必须有已持久化 Run；NEEDS_INPUT 必须有可见阻塞问题。
"""
from __future__ import annotations

import pytest

from wheretogo.agent.decision import (
    ClarificationDraft,
    ContractViolation,
    RunDraft,
    TurnResult,
    build_runtime_decision,
    running_reply,
    validate_turn_contract,
)
from wheretogo.agent.prerequisites import PrerequisiteResolution
from wheretogo.agent.status import (
    RUN_TERMINAL,
    TURN_TERMINAL,
    TURN_TRANSITIONS,
    RunStatus,
    TurnStatus,
    can_transition_run,
    can_transition_turn,
    user_facing_error,
)
from wheretogo.copilot.turn_schema import TurnDecision


# ---------- 状态转移表 ----------

def test_turn_transitions_follow_design_table():
    assert can_transition_turn("received", "interpreting")
    assert can_transition_turn("interpreting", "needs_input")
    assert can_transition_turn("interpreting", "running")
    assert can_transition_turn("interpreting", "answered")
    assert can_transition_turn("interpreting", "failed")
    assert can_transition_turn("needs_input", "interpreting")  # 用户回答
    assert can_transition_turn("running", "answered")
    assert can_transition_turn("running", "partial")
    assert can_transition_turn("running", "cancelled")
    assert can_transition_turn("partial", "running")  # 继续研究
    assert can_transition_turn("partial", "answered")  # 用户接受当前结果


def test_turn_illegal_transitions_rejected():
    assert not can_transition_turn("received", "running")  # 必须先 interpreting
    assert not can_transition_turn("needs_input", "answered")
    assert not can_transition_turn("answered", "running")
    assert not can_transition_turn("failed", "running")
    assert not can_transition_turn("cancelled", "interpreting")


def test_turn_terminal_states_have_no_outgoing():
    for status in TURN_TERMINAL - {TurnStatus.PARTIAL}:
        assert TURN_TRANSITIONS[status] == set()


def test_run_transitions():
    assert can_transition_run("queued", "running")
    assert can_transition_run("running", "composing")
    assert can_transition_run("composing", "succeeded")
    assert can_transition_run("running", "partial")
    assert not can_transition_run("succeeded", "running")
    for status in RUN_TERMINAL:
        assert not can_transition_run(status, RunStatus.RUNNING)


# ---------- 合约校验（§17.1 五条不变量） ----------

def _answered(reply="好的。") -> TurnResult:
    return TurnResult(status=TurnStatus.ANSWERED, visible_reply=reply)


def test_running_requires_persisted_run():
    result = TurnResult(
        status=TurnStatus.RUNNING,
        visible_reply="已创建研究任务。",
        run=RunDraft(run_type="research", goal="g"),
        run_persisted=False,
    )
    with pytest.raises(ContractViolation):
        validate_turn_contract(result)
    result.run_persisted = True
    result.run_id = "run_x"
    validate_turn_contract(result)  # 不抛


def test_needs_input_requires_visible_blocking_question():
    result = TurnResult(status=TurnStatus.NEEDS_INPUT, visible_reply="需要信息")
    with pytest.raises(ContractViolation):
        validate_turn_contract(result)
    # 非阻塞澄清不满足 NEEDS_INPUT
    result.clarification = ClarificationDraft(question="q", blocking=False)
    with pytest.raises(ContractViolation):
        validate_turn_contract(result)
    # 阻塞但问题为空同样违约
    result.clarification = ClarificationDraft(question="  ", blocking=True)
    with pytest.raises(ContractViolation):
        validate_turn_contract(result)
    result.clarification = ClarificationDraft(question="你从哪个城市出发？", blocking=True)
    validate_turn_contract(result)


def test_answered_requires_nonempty_reply():
    with pytest.raises(ContractViolation):
        validate_turn_contract(TurnResult(status=TurnStatus.ANSWERED, visible_reply=""))
    validate_turn_contract(_answered())


def test_terminal_must_have_reply_or_error():
    with pytest.raises(ContractViolation):
        validate_turn_contract(TurnResult(status=TurnStatus.FAILED, visible_reply=""))
    validate_turn_contract(TurnResult(
        status=TurnStatus.FAILED,
        visible_reply="",
        error={"code": "RUN_CREATION_FAILED"},
        error_code="RUN_CREATION_FAILED",
    ))


def test_no_state_may_lack_answer_question_run_and_error():
    empty = TurnResult(status=TurnStatus.RUNNING, visible_reply="")
    with pytest.raises(ContractViolation):
        validate_turn_contract(empty)


# ---------- 承诺语言由 RUNNING 模板控制（§8.3） ----------

def test_running_reply_generated_from_run_not_model():
    reply = running_reply("research", "核实四个地点并生成市内路线", ["按本周末安排"])
    assert "已创建研究任务" in reply
    assert "核实四个地点并生成市内路线" in reply
    assert "按本周末安排" in reply


def test_answered_decision_uses_model_reply_without_promise_template():
    interpreted = TurnDecision(
        primary_intent="ask_info",
        acts=["answer_info"],
        assistant_reply="外滩晚上七点后人比较多。",
        proposed_actions=[{"type": "answer", "reason": "回答"}],
    )
    resolution = PrerequisiteResolution(
        executable_actions=[{"type": "answer", "goal": "", "assumptions": []}],
    )
    result = build_runtime_decision(interpreted, resolution)
    assert result.status == TurnStatus.ANSWERED
    assert result.visible_reply == "外滩晚上七点后人比较多。"
    assert "已创建" not in result.visible_reply  # 非 RUNNING 不允许承诺模板
    assert result.run is None


def test_running_decision_overrides_model_wording_with_template():
    interpreted = TurnDecision(
        primary_intent="provide_constraints",
        acts=["research_more"],
        assistant_reply="我马上查，很快就好！",
        goals=[{"id": "g1", "objective": "找适合亲子的展览", "required": True}],
        proposed_actions=[{"type": "research", "reason": "需要核实"}],
    )
    resolution = PrerequisiteResolution(
        executable_actions=[{"type": "research", "goal": "找适合亲子的展览", "assumptions": []}],
    )
    result = build_runtime_decision(interpreted, resolution)
    assert result.status == TurnStatus.RUNNING
    # 运行中回复由模板生成，模型只贡献任务摘要
    assert "已创建研究任务" in result.visible_reply
    assert "找适合亲子的展览" in result.visible_reply


def test_error_codes_map_to_user_facing_messages():
    err = user_facing_error("RUN_CREATION_FAILED")
    assert err["code"] == "RUN_CREATION_FAILED"
    assert err["message"]
    assert err["recovery"]
    unknown = user_facing_error("NOPE")
    assert unknown["message"]
