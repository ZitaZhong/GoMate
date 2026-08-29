"""Runtime Decision 与合约校验（v4 §8）。

模型只提供语气和任务摘要；是否可以说"正在研究"由真实 Run 创建结果决定。
承诺语言由 RUNNING 模板生成，从源头消除"语言说会做、系统没做"的可能。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .prerequisites import PrerequisiteResolution
from .status import RunType, TurnStatus

#: 运行中回复模板（v4 §8.3：只有 RUNNING 模板允许承诺语言）
_RUN_TYPE_LABEL = {
    RunType.RESEARCH.value: "研究",
    RunType.RECOMPOSE.value: "行程重排",
    RunType.ANSWER.value: "回答",
    RunType.REPLAN.value: "重新规划",
}


class ContractViolation(AssertionError):
    """Turn 合约不变量被破坏（提交响应前必须捕获，不允许静默终止）。"""


@dataclass
class RunDraft:
    """待持久化的 Run 草案；execution_plan.mode 决定 Worker 的执行方式。"""

    run_type: str
    goal: str
    execution_plan: dict = field(default_factory=dict)
    required_inputs: dict = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    parent_run_id: str | None = None
    replaces_run_id: str | None = None  # 置父 Run cancel_requested 的替换语义


@dataclass
class ClarificationDraft:
    """待持久化的澄清草案。"""

    question: str
    reason: str = ""
    blocking: bool = False
    requested_facts: list[dict] = field(default_factory=list)
    assumptions_if_skipped: list[str] = field(default_factory=list)


@dataclass
class TurnResult:
    """一次 Turn 的运行时决定；validate_turn_contract 的校验对象。"""

    status: TurnStatus
    visible_reply: str | None = None
    run: RunDraft | None = None
    clarification: ClarificationDraft | None = None
    error_code: str | None = None
    error: dict | None = None
    run_persisted: bool = False
    run_id: str | None = None


def running_reply(run_type: str, goal: str, assumptions: list[str] | None = None) -> str:
    """RUNNING 状态的回复由运行时按 Run 生成；模型只贡献任务摘要（goal）。"""
    label = _RUN_TYPE_LABEL.get(run_type, "执行")
    summary = (goal or "").strip()
    reply = f"已创建{label}任务" + (f"：{summary}" if summary else "") + "。"
    reply += "我会实时汇报进度，完成后给出结果与依据。"
    for assumption in assumptions or []:
        reply += f"（默认{assumption}）"
    return reply


def build_runtime_decision(
    interpreted,
    resolution: PrerequisiteResolution,
    *,
    has_research_result: bool = False,
    active_run: dict | None = None,
) -> TurnResult:
    """把 Interpreter 输出 + 前置条件解析转成确定性的 Turn 决定（v4 §8.2）。

    决策顺序：可执行的研究/交通/重规划 → RUNNING；可执行重排 → RUNNING(recompose)；
    仅回答 → ANSWERED；无可执行且存在阻塞缺失 → NEEDS_INPUT；否则 ANSWERED。
    """
    executable = list(resolution.executable_actions)
    blocking = list(resolution.blocking_missing)
    non_blocking = list(resolution.non_blocking_missing)
    model_reply = str(getattr(interpreted, "assistant_reply", None) or "").strip()
    goal_text = "；".join(
        str(g.get("objective") or "").strip()
        for g in (getattr(interpreted, "goals", None) or [])
        if str(g.get("objective") or "").strip()
    ) or str(getattr(interpreted, "research_goal", None) or "").strip()

    # 非阻塞澄清：不阻断 Run，只提示补充后能优化什么。
    non_blocking_clar: ClarificationDraft | None = None
    if non_blocking:
        first = non_blocking[0]
        non_blocking_clar = ClarificationDraft(
            question=str(first.get("hint") or "").strip(),
            reason=str(first.get("reason") or "").strip(),
            blocking=False,
            requested_facts=[
                {"name": item.get("fact"), "description": item.get("reason")}
                for item in non_blocking
            ],
            assumptions_if_skipped=[],
        )

    run_kinds = {action["type"] for action in executable}
    # 1) 需要外部执行的动作 → RUNNING（真实 Run 由事务层持久化后才允许承诺）。
    if run_kinds & {"research", "transport_search", "replan"}:
        run_type = (
            RunType.REPLAN.value if "replan" in run_kinds else RunType.RESEARCH.value
        )
        assumptions: list[str] = []
        for action in executable:
            assumptions.extend(action.get("assumptions") or [])
        # 部分执行（v4 §7.3）：阻塞的动作转为非阻塞提示——能做的先做。
        if blocking:
            hints = [
                str(item.get("question") or "").strip()
                for item in blocking
                if str(item.get("question") or "").strip()
            ]
            extra_hint = (
                "另外，" + "".join(hints) + "回答后我可以补充这部分。"
                if hints else ""
            )
            if non_blocking_clar is None and extra_hint:
                non_blocking_clar = ClarificationDraft(
                    question=extra_hint,
                    reason="部分动作缺少必需信息，先执行可完成部分",
                    blocking=False,
                    requested_facts=[
                        {"name": item.get("fact"), "description": item.get("reason")}
                        for item in blocking
                    ],
                )
        draft = RunDraft(
            run_type=run_type,
            goal=goal_text,
            assumptions=list(dict.fromkeys(assumptions)),
        )
        return TurnResult(
            status=TurnStatus.RUNNING,
            visible_reply=running_reply(run_type, goal_text, draft.assumptions),
            run=draft,
            clarification=non_blocking_clar,
        )

    # 2) 只用已有候选重排 → RUNNING(recompose)，不启动外部搜索。
    if "compose_itinerary" in run_kinds and has_research_result:
        draft = RunDraft(run_type=RunType.RECOMPOSE.value, goal=goal_text)
        return TurnResult(
            status=TurnStatus.RUNNING,
            visible_reply=running_reply(RunType.RECOMPOSE.value, goal_text),
            run=draft,
            clarification=non_blocking_clar,
        )

    # 3) 阻塞缺失且没有任何可安全执行的部分 → NEEDS_INPUT（问题必须可见）。
    if blocking and not executable:
        first = blocking[0]
        question = str(first.get("question") or "").strip() or "请补充必要信息。"
        clar = ClarificationDraft(
            question=question,
            reason=str(first.get("reason") or "").strip(),
            blocking=True,
            requested_facts=[
                {
                    "name": item.get("fact"),
                    "description": item.get("reason"),
                    "required_by": item.get("required_by") or [],
                    "acceptable_default": None,
                }
                for item in blocking
            ],
        )
        reply = f"要继续这项任务，我还需要一个信息：{question}"
        # NEEDS_INPUT 不使用模型草稿：草稿可能含“我这就查”等承诺语言，
        # 而此时并没有真实 Run；承诺只允许出现在 RUNNING 模板中（v4 §8.3）。
        return TurnResult(
            status=TurnStatus.NEEDS_INPUT,
            visible_reply=reply,
            clarification=clar,
        )

    # 4) 直接回答（answer/booking/纯闲聊）→ ANSWERED；回复不得使用承诺模板。
    reply = model_reply
    if not reply:
        reply = "好的。想继续的话，告诉我你的目的地、时间或想体验什么。"
    return TurnResult(
        status=TurnStatus.ANSWERED,
        visible_reply=reply,
        clarification=non_blocking_clar,
    )


def validate_turn_contract(result: TurnResult) -> None:
    """强制不变量（v4 §8.3）；违反即抛 ContractViolation，不允许静默终止。"""
    if result.status == TurnStatus.RUNNING:
        if not result.run_persisted or not result.run_id:
            raise ContractViolation("RUNNING 回合必须已持久化真实 Run")
    if result.status == TurnStatus.NEEDS_INPUT:
        if not result.clarification or not result.clarification.blocking:
            raise ContractViolation("NEEDS_INPUT 必须携带阻塞澄清")
        if not (result.clarification.question or "").strip():
            raise ContractViolation("阻塞澄清必须有可见问题")
    if result.status == TurnStatus.ANSWERED:
        if not (result.visible_reply or "").strip():
            raise ContractViolation("ANSWERED 必须有非空回复")
    terminal = {
        TurnStatus.ANSWERED,
        TurnStatus.PARTIAL,
        TurnStatus.FAILED,
        TurnStatus.CANCELLED,
    }
    if (
        result.status in terminal
        and not (result.visible_reply or "").strip()
        and not result.error
    ):
        raise ContractViolation("终态回合不允许既无回复也无错误")
    if (
        not (result.visible_reply or "").strip()
        and not (result.clarification and result.clarification.question)
        and not result.run_id
        and not result.error
    ):
        raise ContractViolation("回合不允许同时缺少 answer/question/run/error")
