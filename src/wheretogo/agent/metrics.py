"""业务指标聚合（v4 §14.2）。

从 Turn/Run/Clarification 表聚合关键健康指标；静默失败类指标目标必须为 0。
"""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import AgentRun, AgentTurn, ClarificationRequest
from .status import TURN_TERMINAL


def collect_metrics(session: Session | None = None) -> dict:
    if session is not None:
        return _collect(session)
    with get_session() as s:
        return _collect(s)


def _collect(s: Session) -> dict:
    terminal_values = [st.value for st in TURN_TERMINAL]
    turn_total = s.query(func.count(AgentTurn.id)).scalar() or 0
    # 静默终态：终态但既无回复、无错误，也没有绑定 Run（目标必须为 0）
    silent_terminal = (
        s.query(func.count(AgentTurn.id))
        .filter(
            AgentTurn.status.in_(terminal_values),
            func.coalesce(AgentTurn.visible_reply, "") == "",
            AgentTurn.error_code.is_(None),
            AgentTurn.run_id.is_(None),
        )
        .scalar()
    ) or 0
    # 承诺未来工作但没有 Run：RUNNING 状态必须绑定 run_id（目标必须为 0）
    promised_without_run = (
        s.query(func.count(AgentTurn.id))
        .filter(AgentTurn.status == "running", AgentTurn.run_id.is_(None))
        .scalar()
    ) or 0
    # 阻塞澄清但 Turn 未标记 needs_input（UI 不可见风险；目标必须为 0）
    hidden_clarification = (
        s.query(func.count(ClarificationRequest.id))
        .join(AgentTurn, ClarificationRequest.turn_id == AgentTurn.id)
        .filter(
            ClarificationRequest.blocking.is_(True),
            ClarificationRequest.status == "open",
            AgentTurn.status != "needs_input",
        )
        .scalar()
    ) or 0
    # Turn 接收 → Run 创建的平均耗时（ms）
    run_start_latency_ms = s.query(
        func.avg(
            func.extract("epoch", AgentRun.created_at - AgentTurn.created_at) * 1000
        )
    ).join(AgentTurn, AgentRun.turn_id == AgentTurn.id).scalar()
    clar_total = s.query(func.count(ClarificationRequest.id)).scalar() or 0
    clar_blocking = (
        s.query(func.count(ClarificationRequest.id))
        .filter(ClarificationRequest.blocking.is_(True))
        .scalar()
    ) or 0
    run_counts = dict(
        s.query(AgentRun.status, func.count(AgentRun.id))
        .group_by(AgentRun.status)
        .all()
    )
    turn_counts = dict(
        s.query(AgentTurn.status, func.count(AgentTurn.id))
        .group_by(AgentTurn.status)
        .all()
    )
    return {
        "turn_total": int(turn_total),
        "turn_silent_terminal_total": int(silent_terminal),
        "promised_without_run_total": int(promised_without_run),
        "hidden_clarification_total": int(hidden_clarification),
        "run_start_latency_ms": (
            round(float(run_start_latency_ms), 1)
            if run_start_latency_ms is not None else None
        ),
        "clarification_blocking_rate": (
            round(clar_blocking / clar_total, 3) if clar_total else None
        ),
        "turn_status_counts": {str(k): int(v) for k, v in turn_counts.items()},
        "run_status_counts": {str(k): int(v) for k, v in run_counts.items()},
    }
