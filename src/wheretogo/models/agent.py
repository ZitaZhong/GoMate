"""v4 回合状态机与任务生命周期模型（技术方案 v4 §6）。

- `AgentTurn`：一次对话回合的持久化事务（状态机 + 幂等键）。
- `AgentRun`：真实、持久化、可查询的运行实例（research/recompose/answer/replan）。
- `AgentRunEvent`：单调递增事件流，SSE Last-Event-ID 续传的事实源。
- `ClarificationRequest`：阻塞/非阻塞澄清，刷新后仍可恢复。
- `AgentOutbox`：Turn 事务内写入，Worker FOR UPDATE SKIP LOCKED 领取。

无新 PG ENUM：status/run_type/topic 用 TEXT + 应用枚举校验（wheretogo.agent.status）。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base


class AgentTurn(Base):
    """一次对话回合 = 一条持久化事务（v4 §6.2）。"""

    __tablename__ = "agent_turns"
    __table_args__ = (
        UniqueConstraint("plan_id", "sequence_no", name="uq_agent_turns_plan_seq"),
        Index("ix_agent_turns_plan_status", "plan_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("plans.id", ondelete="CASCADE")
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="received")
    interpretation: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    visible_reply: Mapped[str | None] = mapped_column(Text)
    clarification_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    error_code: Mapped[str | None] = mapped_column(Text)
    client_key: Mapped[str | None] = mapped_column(Text)  # Idempotency-Key
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentRun(Base):
    """真实运行实例（v4 §6.4）；checkpoint_ref = LangGraph thread_id。"""

    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_plan_status", "plan_id", "status"),
        Index("ix_agent_runs_status_heartbeat", "status", "heartbeat_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("plans.id", ondelete="CASCADE")
    )
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_turns.id", ondelete="CASCADE")
    )
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    run_type: Mapped[str] = mapped_column(Text, nullable=False)  # research/recompose/answer/replan
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    goal: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    execution_plan: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    required_inputs: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    assumptions: Mapped[list] = mapped_column(JSONB, server_default="[]")
    checkpoint_ref: Mapped[str | None] = mapped_column(Text)
    result_bundle_id: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentRunEvent(Base):
    """Run 事件流（v4 §6.5）；(run_id, sequence) 单调递增可续传。"""

    __tablename__ = "agent_run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_agent_run_events_seq"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    phase: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClarificationRequest(Base):
    """澄清请求（v4 §6.3）；blocking 决定是否阻塞 Turn。"""

    __tablename__ = "clarification_requests"
    __table_args__ = (Index("ix_clarification_turn_status", "turn_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    turn_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_turns.id", ondelete="CASCADE"), nullable=False
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, server_default="")
    blocking: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    requested_facts: Mapped[list] = mapped_column(JSONB, server_default="[]")
    assumptions_if_skipped: Mapped[list] = mapped_column(JSONB, server_default="[]")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    answer_turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentOutbox(Base):
    """Outbox（v4 §9.2）；与 Turn/Run 同事务写入，Worker 轮询领取。"""

    __tablename__ = "agent_outbox"
    __table_args__ = (Index("ix_agent_outbox_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    topic: Mapped[str] = mapped_column(Text, nullable=False)  # agent_run.requested
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


__all__ = [
    "AgentTurn",
    "AgentRun",
    "AgentRunEvent",
    "ClarificationRequest",
    "AgentOutbox",
]
