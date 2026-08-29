"""独立 Worker 进程：Outbox 轮询 + Run 执行 + Stalled 检测（v4 §9.2/§13.3）。

启动：``python -m wheretogo.agent.worker``（与 BFF 共库部署，通过 Outbox 表解耦）。
测试中不起进程：直接调用 ``poll_once()`` / ``scan_stalled()`` 同步驱动。
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..models import AgentOutbox, AgentRun
from . import events
from .status import RUN_ACTIVE, RUN_TERMINAL, ErrorCode, RunStatus, TurnStatus
from .supervisor import _finalize, execute_run

logger = logging.getLogger(__name__)


def _claim_next(session: Session) -> tuple[int, str] | None:
    """FOR UPDATE SKIP LOCKED 领取一条可执行任务；child run 等父任务终态。"""
    rows = (
        session.query(AgentOutbox)
        .filter(AgentOutbox.status == "pending", AgentOutbox.topic == "agent_run.requested")
        .order_by(AgentOutbox.created_at.asc())
        .with_for_update(skip_locked=True)
        .limit(20)
        .all()
    )
    for row in rows:
        run_id = str((row.payload or {}).get("run_id") or "")
        if not run_id:
            row.status = "failed"
            row.updated_at = datetime.now(timezone.utc)
            continue
        run = session.get(AgentRun, uuid.UUID(run_id))
        if run is None:
            row.status = "failed"
            row.updated_at = datetime.now(timezone.utc)
            continue
        if run.parent_run_id is not None:
            parent = session.get(AgentRun, run.parent_run_id)
            if parent is not None and RunStatus(parent.status) not in RUN_TERMINAL:
                continue  # 父任务未结束：保持 pending，稍后再领
        row.status = "claimed"
        row.claimed_at = datetime.now(timezone.utc)
        row.attempts = (row.attempts or 0) + 1
        row.updated_at = datetime.now(timezone.utc)
        session.flush()
        return row.id, run_id
    return None


def poll_once(session: Session | None = None, planner=None) -> str | None:
    """领取并执行一条 Outbox 任务；返回最终 Run 状态（无任务→None）。"""
    settings = get_settings()
    if session is not None:
        claimed = _claim_next(session)
        if claimed is None:
            return None
        outbox_id, run_id = claimed
        return _execute_claimed(outbox_id, run_id, session=session, planner=planner,
                                max_retries=settings.agent_run_max_retries)
    with get_session() as s:
        claimed = _claim_next(s)
    if claimed is None:
        return None
    outbox_id, run_id = claimed
    return _execute_claimed(outbox_id, run_id, session=None, planner=planner,
                            max_retries=settings.agent_run_max_retries)


def _execute_claimed(
    outbox_id: int,
    run_id: str,
    *,
    session: Session | None,
    planner,
    max_retries: int,
) -> str:
    try:
        status = execute_run(run_id, session=session, planner=planner)
    except Exception:
        logger.exception("run execution raised run=%s", run_id)
        status = None
    if session is not None:
        return _settle_outbox(session, outbox_id, run_id, status, max_retries)
    with get_session() as s:
        return _settle_outbox(s, outbox_id, run_id, status, max_retries)


def _settle_outbox(
    session: Session,
    outbox_id: int,
    run_id: str,
    status: str | None,
    max_retries: int,
) -> str:
    row = session.get(AgentOutbox, outbox_id)
    now = datetime.now(timezone.utc)
    if status is not None:
        if row is not None:
            row.status = "done"
            row.updated_at = now
        return status
    # 执行异常：按 attempts 重试；超限 → 失败并给用户可读错误。
    if row is not None and (row.attempts or 0) <= max_retries:
        row.status = "pending"
        row.claimed_at = None
        row.updated_at = now
        return "retrying"
    if row is not None:
        row.status = "failed"
        row.updated_at = now
    run = session.get(AgentRun, uuid.UUID(run_id))
    if run is not None and RunStatus(run.status) not in RUN_TERMINAL:
        run.retry_count = (run.retry_count or 0) + 1
        _finalize(
            session, run.id,
            run_status=RunStatus.FAILED,
            turn_status=TurnStatus.FAILED,
            visible_reply="任务多次执行失败，已停止。请重发消息重新发起。",
            error_code=ErrorCode.RUN_STALLED.value,
        )
    return "failed"


def scan_stalled(session: Session | None = None) -> int:
    """心跳超时的活跃 Run：按 retry_count 从 checkpoint 重试或标记失败（§13.3）。"""
    settings = get_settings()
    threshold = datetime.now(timezone.utc) - timedelta(
        seconds=settings.agent_run_stall_threshold_s
    )

    def _scan(s: Session) -> int:
        handled = 0
        rows = (
            s.query(AgentRun)
            .filter(
                AgentRun.status.in_([st.value for st in RUN_ACTIVE]),
                AgentRun.heartbeat_at.isnot(None),
                AgentRun.heartbeat_at < threshold,
            )
            .with_for_update(skip_locked=True)
            .limit(20)
            .all()
        )
        for run in rows:
            handled += 1
            if (run.retry_count or 0) < settings.agent_run_max_retries:
                run.retry_count = (run.retry_count or 0) + 1
                run.status = RunStatus.QUEUED.value
                run.heartbeat_at = None
                events.publish(
                    s, run.id, "run.status",
                    phase="requeued",
                    message="任务疑似停滞，正在从检查点恢复重试",
                    payload={"retry_count": run.retry_count},
                )
                s.add(AgentOutbox(
                    topic="agent_run.requested",
                    payload={"run_id": str(run.id), "plan_id": str(run.plan_id)},
                    status="pending",
                ))
            else:
                _finalize(
                    s, run.id,
                    run_status=RunStatus.FAILED,
                    turn_status=TurnStatus.FAILED,
                    visible_reply="任务长时间没有进展，已停止。请重发消息重新发起研究。",
                    error_code=ErrorCode.RUN_STALLED.value,
                )
        return handled

    if session is not None:
        return _scan(session)
    with get_session() as s:
        return _scan(s)


def main() -> None:
    """Worker 主循环：Outbox 轮询 + 周期 stalled 扫描。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    logger.info("agent worker started (poll=%ss stall=%ss)",
                settings.agent_outbox_poll_interval_s, settings.agent_run_stall_threshold_s)
    last_stall_scan = 0.0
    while True:
        try:
            status = poll_once()
        except Exception:
            logger.exception("worker poll failed")
            status = None
        now = time.monotonic()
        if now - last_stall_scan > max(15.0, settings.agent_run_stall_threshold_s / 2):
            try:
                scan_stalled()
            except Exception:
                logger.exception("stalled scan failed")
            last_stall_scan = now
        if status is None:
            time.sleep(settings.agent_outbox_poll_interval_s)


if __name__ == "__main__":
    main()
