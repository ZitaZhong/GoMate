"""用户与计划域模型（DD-01 §6）。"""
from __future__ import annotations

from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..enums import PlanStage
from ..db.base import Base
from ..db.pgtypes import pg_enum


class User(Base):
    """用户（隐私优先：默认匿名，不存手机号/真实姓名于主表）。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    anon_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserContext(Base):
    """用户长期上下文（v0.1 仅此表，v0.2 再上 Mem0；增补 C）。"""

    __tablename__ = "user_context"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    home_cities: Mapped[list] = mapped_column(JSONB, server_default="[]")
    budget_band: Mapped[dict | None] = mapped_column(JSONB)
    prefer_flight: Mapped[bool | None] = mapped_column(Boolean)
    accept_night_train: Mapped[bool | None] = mapped_column(Boolean)
    interests: Mapped[list] = mapped_column(JSONB, server_default="[]")
    dietary: Mapped[list] = mapped_column(JSONB, server_default="[]")
    visited: Mapped[list] = mapped_column(JSONB, server_default="[]")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Plan(Base):
    """一次周末规划 = 一条 plan；thread_id 关联 LangGraph 检查点（DD-02）。"""

    __tablename__ = "plans"
    __table_args__ = (Index("ix_plans_organizer_created", "organizer_user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organizer_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    stage: Mapped[PlanStage] = mapped_column(
        pg_enum(PlanStage, "plan_stage"), nullable=False, server_default=PlanStage.explore.value
    )
    thread_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)  # = 'plan:{id}'
    constraints: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    conversation: Mapped[list] = mapped_column(JSONB, server_default="[]")  # DD-15 多轮消息兜底（主存 checkpoint）
    weekend_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    weekend_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlanMember(Base):
    """同行人（匿名邀请；v0.1 仅组织者一条）。"""

    __tablename__ = "plan_members"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    invite_token: Mapped[str | None] = mapped_column(Text, unique=True)
    anon_label: Mapped[str | None] = mapped_column(Text)
    is_organizer: Mapped[bool] = mapped_column(Boolean, server_default="false")
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PartyConstraint(Base):
    """同行人约束（脱敏粒度：商圈级而非门牌；个人预算不外显）。"""

    __tablename__ = "party_constraints"
    __table_args__ = (Index("ix_party_constraints_plan", "plan_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    member_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("plan_members.id", ondelete="CASCADE")
    )
    origin_area: Mapped[str | None] = mapped_column(Text)  # "上海·徐汇"（商圈级，脱敏）
    origin_geo: Mapped[object | None] = mapped_column(Geography(geometry_type="POINT", srid=4326))
    earliest_depart: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latest_return: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    budget_band: Mapped[dict | None] = mapped_column(JSONB)
    prefer_flight: Mapped[bool | None] = mapped_column(Boolean)
    accept_night_train: Mapped[bool | None] = mapped_column(Boolean)
    prefs: Mapped[list] = mapped_column(JSONB, server_default="[]")
    dietary: Mapped[list] = mapped_column(JSONB, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


__all__ = ["User", "UserContext", "Plan", "PlanMember", "PartyConstraint"]
