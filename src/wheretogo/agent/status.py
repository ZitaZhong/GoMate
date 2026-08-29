"""v4 状态机与错误码（技术方案 v4 §4.4 / §6.1 / §6.4 / §13.1）。

模型理解开放世界；这里约束的是有限、可验证的执行状态空间。
"""
from __future__ import annotations

from enum import Enum


class TurnStatus(str, Enum):
    """一个 Turn 的用户可感知状态（v4 §6.1）。"""

    RECEIVED = "received"
    INTERPRETING = "interpreting"
    NEEDS_INPUT = "needs_input"
    RUNNING = "running"
    ANSWERED = "answered"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: v4 §6.1 允许的状态转移（不在表内的转移一律非法）
TURN_TRANSITIONS: dict[TurnStatus, set[TurnStatus]] = {
    TurnStatus.RECEIVED: {TurnStatus.INTERPRETING},
    TurnStatus.INTERPRETING: {
        TurnStatus.NEEDS_INPUT,
        TurnStatus.RUNNING,
        TurnStatus.ANSWERED,
        TurnStatus.FAILED,
    },
    TurnStatus.NEEDS_INPUT: {TurnStatus.INTERPRETING},  # 用户回答后重新解释
    TurnStatus.RUNNING: {
        TurnStatus.ANSWERED,
        TurnStatus.PARTIAL,
        TurnStatus.FAILED,
        TurnStatus.CANCELLED,
    },
    TurnStatus.PARTIAL: {TurnStatus.RUNNING, TurnStatus.ANSWERED},
    TurnStatus.ANSWERED: set(),
    TurnStatus.FAILED: set(),
    TurnStatus.CANCELLED: set(),
}

#: Turn 终态（进入后不再变化；NEEDS_INPUT 是可恢复驻留态，不算终态）
TURN_TERMINAL = {
    TurnStatus.ANSWERED,
    TurnStatus.PARTIAL,
    TurnStatus.FAILED,
    TurnStatus.CANCELLED,
}


def can_transition_turn(src: TurnStatus | str, dst: TurnStatus | str) -> bool:
    src = TurnStatus(src)
    dst = TurnStatus(dst)
    return dst in TURN_TRANSITIONS.get(src, set())


class RunStatus(str, Enum):
    """AgentRun 状态（v4 §6.4）。"""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_TOOL = "waiting_tool"
    COMPOSING = "composing"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: 活跃 Run（受 heartbeat/stall 监控）
RUN_ACTIVE = {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_TOOL, RunStatus.COMPOSING}
#: Run 终态
RUN_TERMINAL = {RunStatus.SUCCEEDED, RunStatus.PARTIAL, RunStatus.FAILED, RunStatus.CANCELLED}

RUN_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED},
    RunStatus.RUNNING: {
        RunStatus.WAITING_TOOL,
        RunStatus.COMPOSING,
        RunStatus.SUCCEEDED,
        RunStatus.PARTIAL,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.WAITING_TOOL: {
        RunStatus.RUNNING,
        RunStatus.COMPOSING,
        RunStatus.PARTIAL,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.COMPOSING: {
        RunStatus.SUCCEEDED,
        RunStatus.PARTIAL,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.SUCCEEDED: set(),
    RunStatus.PARTIAL: set(),
    RunStatus.FAILED: set(),
    RunStatus.CANCELLED: set(),
}


def can_transition_run(src: RunStatus | str, dst: RunStatus | str) -> bool:
    src = RunStatus(src)
    dst = RunStatus(dst)
    return dst in RUN_TRANSITIONS.get(src, set())


class RunType(str, Enum):
    """Run 的可执行类型（v4 §6.4；封闭动作空间，语义空间保持开放）。"""

    RESEARCH = "research"
    RECOMPOSE = "recompose"
    ANSWER = "answer"
    REPLAN = "replan"


class ErrorCode(str, Enum):
    """失败分类（v4 §13.1）；每种错误映射用户可理解信息与恢复动作。"""

    INTERPRETATION_FAILED = "INTERPRETATION_FAILED"
    PREREQUISITE_BLOCKED = "PREREQUISITE_BLOCKED"
    RUN_CREATION_FAILED = "RUN_CREATION_FAILED"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_AUTH_FAILED = "TOOL_AUTH_FAILED"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PARTIAL_EVIDENCE = "PARTIAL_EVIDENCE"
    COMPOSITION_FAILED = "COMPOSITION_FAILED"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
    RUN_STALLED = "RUN_STALLED"


#: 错误码 → (用户可理解信息, 可执行恢复动作)
ERROR_MESSAGES: dict[ErrorCode, tuple[str, str]] = {
    ErrorCode.INTERPRETATION_FAILED: (
        "我没有理解这句话的意图。", "请换一种说法，或补充你想去哪里、什么时候。"),
    ErrorCode.PREREQUISITE_BLOCKED: (
        "缺少继续执行所必需的信息。", "请回答上面的问题后继续。"),
    ErrorCode.RUN_CREATION_FAILED: (
        "研究任务创建失败，尚未开始执行。", "请重发这条消息重试。"),
    ErrorCode.TOOL_TIMEOUT: (
        "部分信息来源响应超时，结果可能不完整。", "可以让我继续补充研究缺失的部分。"),
    ErrorCode.TOOL_AUTH_FAILED: (
        "外部数据服务认证失败。", "请检查服务配置后重试。"),
    ErrorCode.PROVIDER_RATE_LIMITED: (
        "外部数据服务限流。", "请稍等一会儿再重试。"),
    ErrorCode.PARTIAL_EVIDENCE: (
        "只获得了部分可核实的信息。", "可以继续研究补齐缺口，或基于当前结果先规划。"),
    ErrorCode.COMPOSITION_FAILED: (
        "研究已完成，但生成最终回复失败。", "候选结果已保留，可以只重试生成回复。"),
    ErrorCode.PERSISTENCE_FAILED: (
        "结果保存失败。", "请重试；已获得的研究不会重复搜索。"),
    ErrorCode.RUN_STALLED: (
        "任务长时间没有进展，已停止。", "请重发消息重新发起研究。"),
}


def user_facing_error(code: ErrorCode | str | None) -> dict:
    """错误码 → {code, message, recovery}；未知码给通用信息。"""
    try:
        ec = ErrorCode(code) if code else None
    except ValueError:
        ec = None
    if ec is None:
        return {"code": str(code or "UNKNOWN"), "message": "发生未知错误。", "recovery": "请重试。"}
    message, recovery = ERROR_MESSAGES[ec]
    return {"code": ec.value, "message": message, "recovery": recovery}
