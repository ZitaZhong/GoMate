"""Workspace 恢复快照（v4 §11.3）。

页面刷新/断线重连后恢复完整工作区：运行中任务、待澄清问题、当前方案与研究上下文。
绝不只恢复一句"我先查询"。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..db import get_session
from ..models import (
    AgentRun,
    AgentTurn,
    ClarificationRequest,
    Plan,
    TripBundle,
)
from .events import event_dict, read_since
from .persist import agent_results_from_bundle
from .status import RUN_ACTIVE
from .transaction import _clarification_dict, _run_dict


def _turn_dict(turn: AgentTurn | None) -> dict | None:
    if turn is None:
        return None
    return {
        "id": str(turn.id),
        "sequence_no": turn.sequence_no,
        "status": turn.status,
        "user_message": turn.user_message,
        "visible_reply": turn.visible_reply,
        "error_code": turn.error_code,
        "run_id": str(turn.run_id) if turn.run_id else None,
        "clarification_id": str(turn.clarification_id) if turn.clarification_id else None,
        "created_at": turn.created_at.isoformat() if turn.created_at else None,
        "completed_at": turn.completed_at.isoformat() if turn.completed_at else None,
    }


def get_workspace(plan_id: str, session: Session | None = None) -> dict:
    """组装完整 Workspace；plan 不存在抛 LookupError（API 映射 404）。"""
    if session is not None:
        return _build(session, plan_id)
    with get_session() as s:
        return _build(s, plan_id)


def _build(s: Session, plan_id: str) -> dict:
    if not str(plan_id).isdigit():
        raise LookupError(f"plan {plan_id} 不存在")
    plan = s.get(Plan, int(plan_id))
    if plan is None:
        raise LookupError(f"plan {plan_id} 不存在")
    conversation = [
        {
            "role": turn.get("role"),
            "content": turn.get("content"),
            "intent": turn.get("intent"),
            "turn_status": turn.get("turn_status"),
            # design_itinerary 路线卡（DD-15 v1.1）：刷新后前端仍能渲染
            **({"route_plan": turn.get("route_plan")} if turn.get("route_plan") else {}),
        }
        for turn in (plan.conversation or [])
        if turn.get("role") in {"user", "assistant"}
        and str(turn.get("content") or "").strip()
    ]
    latest_turn = (
        s.query(AgentTurn)
        .filter_by(plan_id=plan.id)
        .order_by(AgentTurn.sequence_no.desc())
        .first()
    )
    active_run = (
        s.query(AgentRun)
        .filter(
            AgentRun.plan_id == plan.id,
            AgentRun.status.in_([st.value for st in RUN_ACTIVE]),
        )
        .order_by(AgentRun.created_at.desc())
        .first()
    )
    open_clars = (
        s.query(ClarificationRequest)
        .join(AgentTurn, ClarificationRequest.turn_id == AgentTurn.id)
        .filter(AgentTurn.plan_id == plan.id, ClarificationRequest.status == "open")
        .order_by(ClarificationRequest.created_at.desc())
        .all()
    )
    bundle_row = (
        s.query(TripBundle)
        .filter_by(plan_id=plan.id, version="explore")
        .order_by(TripBundle.created_at.desc())
        .first()
    )
    bundle_payload = dict(bundle_row.payload or {}) if bundle_row else None
    last_event_id = 0
    recent_events: list[dict] = []
    if active_run is not None:
        rows = read_since(s, active_run.id, 0)
        if rows:
            last_event_id = rows[-1].sequence
            recent_events = [event_dict(row) for row in rows[-20:]]
    run_view = _run_dict(active_run)
    if run_view is not None:
        run_view["heartbeat_at"] = (
            active_run.heartbeat_at.isoformat() if active_run.heartbeat_at else None
        )
        run_view["recent_events"] = recent_events
    return {
        "plan_id": str(plan.id),
        "stage": plan.stage,
        "constraints": dict(plan.constraints or {}),
        "conversation": conversation,
        "active_turn": _turn_dict(latest_turn),
        "active_run": run_view,
        "open_clarifications": [_clarification_dict(clar) for clar in open_clars],
        "current_plan": bundle_payload,
        "research_workspace": agent_results_from_bundle(bundle_payload),
        "last_event_id": last_event_id,
    }
