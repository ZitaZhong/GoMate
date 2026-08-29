"""RunEvent 发布与续传读取（v4 §6.5）。

事件序列每 run 单调递增、可重复读取；SSE 断线后用 Last-Event-ID(=sequence) 续传。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import AgentRun, AgentRunEvent


def publish(
    session: Session,
    run_id: uuid.UUID | str,
    type_: str,
    *,
    phase: str | None = None,
    message: str | None = None,
    payload: dict | None = None,
) -> AgentRunEvent:
    """写入下一个序列号的事件，同时刷新 Run 心跳（单写者：Worker）。"""
    rid = uuid.UUID(str(run_id))
    # 锁 Run 行以串行化序列号分配（同一 run 只有一个执行者，锁只是护栏）。
    run = session.query(AgentRun).filter_by(id=rid).with_for_update().one()
    next_seq = (
        session.query(func.coalesce(func.max(AgentRunEvent.sequence), 0))
        .filter(AgentRunEvent.run_id == rid)
        .scalar()
    ) + 1
    event = AgentRunEvent(
        run_id=rid,
        sequence=next_seq,
        type=type_,
        phase=phase,
        message=message,
        payload=dict(payload or {}),
    )
    session.add(event)
    run.heartbeat_at = datetime.now(timezone.utc)
    session.flush()
    return event


def read_since(
    session: Session,
    run_id: uuid.UUID | str,
    after_sequence: int = 0,
    limit: int = 500,
) -> list[AgentRunEvent]:
    """按序读取 sequence > after_sequence 的事件（SSE 续传）。"""
    rid = uuid.UUID(str(run_id))
    return (
        session.query(AgentRunEvent)
        .filter(AgentRunEvent.run_id == rid, AgentRunEvent.sequence > int(after_sequence))
        .order_by(AgentRunEvent.sequence.asc())
        .limit(limit)
        .all()
    )


def event_dict(event: AgentRunEvent) -> dict:
    """稳定 JSON 视图（SSE data / workspace 恢复共用）。"""
    return {
        "event_id": f"evt_{event.run_id.hex[:8]}_{event.sequence}",
        "run_id": str(event.run_id),
        "sequence": event.sequence,
        "type": event.type,
        "phase": event.phase,
        "message": event.message,
        "payload": dict(event.payload or {}),
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }
