"""v4 Agent API（Turn / Run Events / Workspace / Cancel / Trace / Metrics）。

前端只与这组端点交互：发送消息、订阅 Run 事件、恢复 Workspace。
旧端点（/plans/{id}/chat、/stream、/research-more、/agent-state）原样保留为兼容层。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from ..agent import ContractViolation, collect_metrics, commit_turn, get_workspace
from ..agent.events import event_dict, read_since
from ..agent.status import RUN_TERMINAL, RunStatus, TurnStatus
from ..config import get_settings
from ..db import get_session
from ..models import AgentRun, AgentRunEvent, AgentTurn, ClarificationRequest

router = APIRouter(prefix="/agent", tags=["agent-v4"])
logger = logging.getLogger(__name__)


class TurnBody(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    idempotency_key: str | None = None


@router.post("/conversations/{plan_id}/turns")
def create_turn(plan_id: str, body: TurnBody, request: Request):
    """一轮对话 = 一次持久化事务：解释、澄清、Run、Outbox 原子提交（v4 §9.1）。"""
    client_key = (
        request.headers.get("Idempotency-Key")
        or body.idempotency_key
        or None
    )
    try:
        out = commit_turn(plan_id, body.message, client_key=client_key)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="plan 不存在") from exc
    except ContractViolation:
        # 合约校验失败：属于运行时缺陷，绝不返回"HTTP 200 但全空"。
        logger.exception("turn contract violated plan_id=%s", plan_id)
        raise HTTPException(
            status_code=500,
            detail="回合状态校验失败，此轮未提交；请重发消息。",
        )
    status_code = 202 if out.get("turn_status") == TurnStatus.RUNNING.value else 200
    return JSONResponse(out, status_code=status_code)


def _require_run(run_id: str) -> None:
    try:
        rid = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="run 不存在") from exc
    with get_session() as s:
        if s.get(AgentRun, rid) is None:
            raise HTTPException(status_code=404, detail="run 不存在")


@router.get("/runs/{run_id}/events")
def run_events(run_id: str, request: Request, after: int = 0):
    """Run 事件流（SSE）：Last-Event-ID(=sequence) 续传，终态事件后关闭（v4 §6.5）。"""
    _require_run(run_id)
    last_header = request.headers.get("Last-Event-ID") or ""
    try:
        cursor = max(int(last_header), int(after)) if last_header else int(after)
    except ValueError:
        cursor = int(after)
    poll_interval = get_settings().agent_events_poll_interval_s

    def stream():
        position = cursor
        idle_started = time.monotonic()
        while True:
            final_seen = False
            with get_session() as s:
                rows = read_since(s, run_id, position)
                run = s.get(AgentRun, uuid.UUID(run_id))
                run_terminal = (
                    run is not None and RunStatus(run.status) in RUN_TERMINAL
                )
            for row in rows:
                position = row.sequence
                payload = event_dict(row)
                if (row.payload or {}).get("final"):
                    final_seen = True
                yield {
                    "id": str(row.sequence),
                    "event": row.type,
                    "data": json.dumps(payload, ensure_ascii=False, default=str),
                }
            if final_seen:
                return
            if rows:
                idle_started = time.monotonic()
            elif run_terminal:
                # 终态但没有 final 事件（历史数据/异常路径）：补一个收尾事件。
                yield {
                    "id": str(position),
                    "event": "run.status",
                    "data": json.dumps(
                        {"final": True, "status": run.status},
                        ensure_ascii=False,
                    ),
                }
                return
            elif time.monotonic() - idle_started > 900:
                return  # 与 plan_lock TTL 同级的兜底，防止悬挂连接
            time.sleep(poll_interval)

    return EventSourceResponse(stream())


@router.get("/conversations/{plan_id}/workspace")
def workspace(plan_id: str):
    """完整工作区快照：active_run / open_clarifications / 当前方案（v4 §11.3）。"""
    try:
        return get_workspace(plan_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="plan 不存在") from exc


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    """请求取消 Run：执行中由 Supervisor 在检查点收敛；排队中立即取消。"""
    _require_run(run_id)
    from ..agent.supervisor import _finalize

    with get_session() as s:
        run = s.get(AgentRun, uuid.UUID(run_id))
        if RunStatus(run.status) in RUN_TERMINAL:
            return {"ok": True, "status": run.status, "already_terminal": True}
        run.cancel_requested = True
        if run.status == RunStatus.QUEUED.value:
            _finalize(
                s, run.id,
                run_status=RunStatus.CANCELLED,
                turn_status=TurnStatus.CANCELLED,
                visible_reply="任务已按你的要求取消。",
            )
            return {"ok": True, "status": RunStatus.CANCELLED.value}
        return {"ok": True, "status": run.status, "cancel_requested": True}


@router.get("/turns/{turn_id}/trace")
def turn_trace(turn_id: str):
    """开发环境只读 Trace（v4 §14.3）；脱敏：不含密钥与模型隐藏推理。"""
    if get_settings().app_env != "dev":
        raise HTTPException(status_code=404, detail="not found")
    try:
        tid = uuid.UUID(turn_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="turn 不存在") from exc
    with get_session() as s:
        turn = s.get(AgentTurn, tid)
        if turn is None:
            raise HTTPException(status_code=404, detail="turn 不存在")
        run = s.get(AgentRun, turn.run_id) if turn.run_id else None
        clar = (
            s.get(ClarificationRequest, turn.clarification_id)
            if turn.clarification_id else None
        )
        events_view = []
        if run is not None:
            events_view = [
                event_dict(row)
                for row in s.query(AgentRunEvent)
                .filter_by(run_id=run.id)
                .order_by(AgentRunEvent.sequence.asc())
                .limit(500)
                .all()
            ]
        return {
            "turn": {
                "id": str(turn.id),
                "plan_id": str(turn.plan_id),
                "sequence_no": turn.sequence_no,
                "user_message": turn.user_message,
                "status": turn.status,
                "visible_reply": turn.visible_reply,
                "error_code": turn.error_code,
                "created_at": str(turn.created_at),
                "completed_at": str(turn.completed_at) if turn.completed_at else None,
            },
            "interpretation": dict(turn.interpretation or {}),
            "clarification": (
                {
                    "id": str(clar.id),
                    "question": clar.question,
                    "blocking": clar.blocking,
                    "status": clar.status,
                    "requested_facts": list(clar.requested_facts or []),
                }
                if clar else None
            ),
            "run": (
                {
                    "id": str(run.id),
                    "type": run.run_type,
                    "status": run.status,
                    "goal": run.goal,
                    "execution_plan": dict(run.execution_plan or {}),
                    "assumptions": list(run.assumptions or []),
                    "retry_count": run.retry_count,
                    "error_code": run.error_code,
                    "result_bundle_id": run.result_bundle_id,
                }
                if run else None
            ),
            "events": events_view,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }


@router.get("/metrics")
def metrics():
    """关键业务指标（v4 §14.2）；静默失败类指标目标必须为 0。"""
    return collect_metrics()
