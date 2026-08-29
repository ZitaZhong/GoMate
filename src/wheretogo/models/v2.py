"""v2 新增模型（DD-16 记忆 / DD-17 深研 / DD-06 审核队列）。

- `UserMemory`：Mem0 风格长期语义记忆（pgvector + 软失效覆盖语义）。
- `DeepResearchJob` / `DeepResearchCache`：实时深研作业与短 TTL 缓存元数据（结果本身落 activities）。
- `ActivityReviewQueue`：抽取失败/冲突/低置信的人工复核台（DD-06 §5.9）。

无新 PG ENUM：mem_type/trigger/status/reason 用 TEXT + 应用枚举校验，降低迁移耦合。
"""
from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..config import get_settings
from ..db.base import Base

_EMBED_DIM = get_settings().embedding_dim


class UserMemory(Base):
    """长期语义记忆（DD-16 §3；跨会话偏好/历史，Mem0 风格）。"""

    __tablename__ = "user_memory"
    __table_args__ = (Index("ix_user_memory_user_valid", "user_id", "mem_type", "valid"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE")
    )
    mem_type: Mapped[str] = mapped_column(Text, nullable=False)  # preference / fact / episodic
    key: Mapped[str | None] = mapped_column(Text)  # 归一化键，用于同键覆盖
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(_EMBED_DIM))
    confidence: Mapped[float] = mapped_column(Float, server_default="0.7")
    source_plan_id: Mapped[int | None] = mapped_column(BigInteger)
    valid: Mapped[bool] = mapped_column(Boolean, server_default="true")  # 覆盖时旧记忆置 FALSE（软失效）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DeepResearchJob(Base):
    """实时深研作业（DD-17 §5；可观测/幂等/复用）。"""

    __tablename__ = "deep_research_jobs"
    __table_args__ = (Index("ix_deep_research_jobs_plan", "plan_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("plans.id", ondelete="CASCADE")
    )
    trigger: Mapped[str] = mapped_column(Text, nullable=False)  # user_explicit/coverage_gap/stale/long_tail_city
    query: Mapped[dict] = mapped_column(JSONB, nullable=False)  # {city, weekend, categories, nl}
    status: Mapped[str] = mapped_column(Text, server_default="running")  # running/succeeded/partial/no_results/failed/timeout
    found_activity_ids: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    source_count: Mapped[int] = mapped_column(Integer, server_default="0")
    official_count: Mapped[int] = mapped_column(Integer, server_default="0")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class DeepResearchCache(Base):
    """深研缓存（DD-17 §5；相同查询短期复用，控成本）。"""

    __tablename__ = "deep_research_cache"

    query_hash: Mapped[str] = mapped_column(Text, primary_key=True)  # sha256(完整研究语义+版本)
    result_ids: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    source_list: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActivityReviewQueue(Base):
    """活动审核队列（DD-06 §5.9；抽取失败/冲突/低置信/quote_mismatch/geocode_failed）。"""

    __tablename__ = "activity_review_queue"
    __table_args__ = (Index("ix_activity_review_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    raw_page_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("raw_pages.id", ondelete="CASCADE")
    )
    source_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_registry.id", ondelete="SET NULL")
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    draft: Mapped[dict | None] = mapped_column(JSONB)
    conflict_with: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("activities.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(Text, server_default="pending")  # pending/approved/rejected/merged
    reviewer: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


__all__ = ["UserMemory", "DeepResearchJob", "DeepResearchCache", "ActivityReviewQueue"]
