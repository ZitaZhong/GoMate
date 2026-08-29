"""GoMate 活动房间模型（DD-18 §2；市内多人协作域）。

状态字段用 TEXT + 应用侧枚举校验（v2 范式，不新增 PG ENUM）；
出发地坐标沿用 Geography(POINT,4326)（与 party_constraints.origin_geo 一致）。
"""
from __future__ import annotations

from datetime import date, datetime

from geoalchemy2 import Geography
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base


class Room(Base):
    """活动房间（DD-18 §2.1；状态机 8 态见 enums.RoomStatus）。"""

    __tablename__ = "rooms"
    __table_args__ = (Index("ix_rooms_status_expire", "status", "expire_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="DRAFT")
    activity_date: Mapped[date] = mapped_column(Date, nullable=False)
    city: Mapped[str] = mapped_column(Text, nullable=False, server_default="上海")
    time_window: Mapped[dict | None] = mapped_column(JSONB)  # {"earliest","latest"}
    budget_range: Mapped[dict | None] = mapped_column(JSONB)  # {"min","max","currency"}
    theme: Mapped[str | None] = mapped_column(Text)
    theme_method: Mapped[str | None] = mapped_column(Text)  # direct|vote|ai|wheel
    wheel_spins: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    creator_id: Mapped[str] = mapped_column(Text, nullable=False)
    plan_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("plans.id", ondelete="SET NULL")
    )
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)  # = 'room:{id}'
    invite_code: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RoomMember(Base):
    """房间成员（DD-18 §2.2；member_token 轻量认证，出发地商圈级脱敏对外）。"""

    __tablename__ = "room_members"
    __table_args__ = (Index("ix_room_members_room", "room_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False
    )
    nickname: Mapped[str] = mapped_column(Text, nullable=False)
    member_token: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    is_creator: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    origin_name: Mapped[str | None] = mapped_column(Text)  # 地铁站/商圈
    origin_geo: Mapped[object | None] = mapped_column(Geography(geometry_type="POINT", srid=4326))
    origin_poi_id: Mapped[str | None] = mapped_column(Text)
    earliest_depart: Mapped[str | None] = mapped_column(Text)  # "14:00"
    latest_end: Mapped[str | None] = mapped_column(Text)  # "21:00"
    budget: Mapped[int | None] = mapped_column(Integer)  # 人均预算（分）
    interests: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    hard_constraints: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    negative_prefs: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    transport_pref: Mapped[str | None] = mapped_column(Text)  # walk|transit|drive|any
    note: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ThemeVote(Base):
    """主题投票（DD-18 §2.3；weight：1=可接受、3=强烈喜欢、-2=不喜欢）。"""

    __tablename__ = "theme_votes"
    __table_args__ = (
        UniqueConstraint("room_id", "member_id", "theme"),
        Index("ix_theme_votes_room", "room_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False
    )
    member_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("room_members.id", ondelete="CASCADE"), nullable=False
    )
    theme: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RoomItinerary(Base):
    """房间行程版本（DD-18 §2.4/§6；is_current 唯一当前版，保留最近 5 版可撤销）。"""

    __tablename__ = "room_itineraries"
    __table_args__ = (Index("ix_room_itineraries_room_current", "room_id", "is_current"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


__all__ = ["Room", "RoomMember", "ThemeVote", "RoomItinerary"]
