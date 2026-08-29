"""回填与行程域模型（DD-01 §8）。"""
from __future__ import annotations

from datetime import datetime

from geoalchemy2 import Geography
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..enums import BookingKind, BundleVersion, ReminderChannel, ReminderType, SlotKind
from ..db.base import Base
from ..db.pgtypes import pg_enum


class Booking(Base):
    """回填的交通/住宿（BYO Booking；确认后才生效；DD-10）。"""

    __tablename__ = "bookings"
    __table_args__ = (Index("ix_bookings_plan_kind", "plan_id", "kind"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[BookingKind] = mapped_column(pg_enum(BookingKind, "booking_kind"), nullable=False)
    raw_input: Mapped[str | None] = mapped_column(Text)  # 原始文本/截图OSS地址/链接
    input_kind: Mapped[str | None] = mapped_column(Text)  # text/image/link/manual
    extracted: Mapped[dict | None] = mapped_column(JSONB)  # 抽取结果（schema 见 DD-10）
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)  # confirmed_by_user 时定级
    confirmed: Mapped[bool] = mapped_column(Boolean, server_default="false")  # 逐字段确认后才入时间线
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DiningPick(Base):
    """餐饮候选（DD-11；动线上的饭点）。"""

    __tablename__ = "dining_picks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    amap_poi_id: Mapped[str | None] = mapped_column(Text)  # 高德 POI id
    name: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[object | None] = mapped_column(Geography(geometry_type="POINT", srid=4326))
    cuisine: Mapped[str | None] = mapped_column(Text)
    price_band: Mapped[dict | None] = mapped_column(JSONB)
    open_hours: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    meal_slot: Mapped[str | None] = mapped_column(Text)  # lunch/dinner/coffee
    is_fallback: Mapped[bool] = mapped_column(Boolean, server_default="false")  # 稳妥备选
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RouteLeg(Base):
    """接驳路段（DD-11；逐段 门到门）。"""

    __tablename__ = "route_legs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int | None] = mapped_column(Integer)
    from_label: Mapped[str | None] = mapped_column(Text)
    to_label: Mapped[str | None] = mapped_column(Text)
    from_geo: Mapped[object | None] = mapped_column(Geography(geometry_type="POINT", srid=4326))
    to_geo: Mapped[object | None] = mapped_column(Geography(geometry_type="POINT", srid=4326))
    mode: Mapped[str | None] = mapped_column(Text)  # walk/transit/drive/taxi
    minutes: Mapped[int | None] = mapped_column(Integer)
    distance_m: Mapped[int | None] = mapped_column(Integer)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)  # source: amap/rule
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TimelineSlot(Base):
    """时间线槽位（DD-12 求解产出）。"""

    __tablename__ = "timeline_slots"
    __table_args__ = (UniqueConstraint("plan_id", "seq", name="uq_timeline_plan_seq"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kind: Mapped[SlotKind] = mapped_column(pg_enum(SlotKind, "slot_kind"), nullable=False)
    ref_table: Mapped[str | None] = mapped_column(Text)  # activities/bookings/dining/route_legs
    ref_id: Mapped[int | None] = mapped_column(BigInteger)
    title: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)


class TripBundle(Base):
    """Trip Bundle 版本快照（DD-13；探索版/确认版）。"""

    __tablename__ = "trip_bundles"
    __table_args__ = (Index("ix_trip_bundles_plan_ver_created", "plan_id", "version", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[BundleVersion] = mapped_column(pg_enum(BundleVersion, "bundle_version"), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)  # 渲染就绪 bundle（每字段 evidence）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Reminder(Base):
    """提醒（DD-13；Push/Email/ICS）。"""

    __tablename__ = "reminders"
    __table_args__ = (Index("ix_reminders_status_fire", "status", "fire_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[ReminderType] = mapped_column(pg_enum(ReminderType, "reminder_type"), nullable=False)
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    channel: Mapped[ReminderChannel] = mapped_column(
        pg_enum(ReminderChannel, "reminder_channel"), nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, server_default="scheduled")  # scheduled/sent/failed/cancelled
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = ["Booking", "DiningPick", "RouteLeg", "TimelineSlot", "TripBundle", "Reminder"]
