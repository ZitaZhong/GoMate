"""城市与活动域模型（DD-01 §7）。

`activities` 是核心资产：每条 = 带证据的结构化记录，携带 embedding 与 search_tsv 供 DD-05 检索。
读写解耦铁律：`activities` 只由 DD-06 写，规划流（DD-02/DD-05）只读。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from geoalchemy2 import Geography
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, INTERVAL, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from ..config import get_settings
from ..enums import AvailabilityStatus, SourceType, VerificationStatus
from ..db.base import Base
from ..db.pgtypes import pg_enum

_EMBED_DIM = get_settings().embedding_dim


class CityPlaybook(Base):
    """城市档案（人工/运营维护，v0.1 先做 15~20 城）。"""

    __tablename__ = "city_playbook"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    city_code: Mapped[str] = mapped_column(Text, unique=True, nullable=False)  # 高德 adcode
    name: Mapped[str] = mapped_column(Text, nullable=False)
    center: Mapped[object | None] = mapped_column(Geography(geometry_type="POINT", srid=4326))
    stations: Mapped[list | None] = mapped_column(JSONB)
    lodging_areas: Mapped[list | None] = mapped_column(JSONB)
    hubs: Mapped[list | None] = mapped_column(JSONB)
    transit_notes: Mapped[dict | None] = mapped_column(JSONB)
    weekend_tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    seasonal_risk: Mapped[dict | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Venue(Base):
    """场馆/主办方目录（活动的空间锚点）。"""

    __tablename__ = "venues"
    __table_args__ = (UniqueConstraint("city_code", "name", name="uq_venues_city_name"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    city_code: Mapped[str | None] = mapped_column(Text, ForeignKey("city_playbook.city_code"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[object | None] = mapped_column(Geography(geometry_type="POINT", srid=4326))
    category: Mapped[str | None] = mapped_column(Text)  # 博物馆/剧院/体育馆/展馆
    official_url: Mapped[str | None] = mapped_column(Text)


class SourceRegistry(Base):
    """来源注册表（DD-06 情报流水线；v1 §8.2）。"""

    __tablename__ = "source_registry"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    city_code: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[SourceType] = mapped_column(pg_enum(SourceType, "source_type"), nullable=False)
    entry_url: Mapped[str] = mapped_column(Text, nullable=False)
    parser_kind: Mapped[str | None] = mapped_column(Text)  # static/js/rss/api
    fetch_interval: Mapped[timedelta] = mapped_column(INTERVAL, server_default="1 day")
    robots_ok: Mapped[bool] = mapped_column(Boolean, server_default="true")
    trust_level: Mapped[int] = mapped_column(Integer, server_default="3")  # 1~7 可信度分级
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="true")
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RawPage(Base):
    """抓取原始页（供抽取与回溯；DD-06）。"""

    __tablename__ = "raw_pages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_registry.id", ondelete="CASCADE")
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(Text)  # 去重：无变化不重抽
    etag: Mapped[str | None] = mapped_column(Text)
    clean_md: Mapped[str | None] = mapped_column(Text)  # Readability/Jina 清洗后的正文
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Activity(Base):
    """活动（核心资产：带证据的结构化记录；v1 §6.3 + 增补 A）。"""

    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    fingerprint: Mapped[str | None] = mapped_column(Text, unique=True)  # 归一指纹（去重/实体对齐）
    title: Mapped[str] = mapped_column(Text, nullable=False)
    city_code: Mapped[str | None] = mapped_column(Text, ForeignKey("city_playbook.city_code"))
    venue_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("venues.id"))
    venue: Mapped[str | None] = mapped_column(Text)  # 冗余场馆名（抽取原文）
    location: Mapped[object | None] = mapped_column(Geography(geometry_type="POINT", srid=4326))
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    price_text: Mapped[str | None] = mapped_column(Text)  # 原文价格，不做换算臆测
    booking_url: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)  # 展览/演出/赛事/市集...
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)  # §5 标准结构
    verification_status: Mapped[VerificationStatus] = mapped_column(
        pg_enum(VerificationStatus, "verification_status"), nullable=False
    )
    availability_status: Mapped[AvailabilityStatus] = mapped_column(
        pg_enum(AvailabilityStatus, "availability_status"),
        server_default=AvailabilityStatus.user_must_confirm.value,
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(_EMBED_DIM))  # BGE-M3；DD-05
    embedding_version: Mapped[str] = mapped_column(Text, server_default="bge-m3-v1")
    # BM25/全文（增补 A）：GENERATED ALWAYS AS ... STORED，只读
    search_tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(venue,''))",
            persisted=True,
        ),
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 过期自动下架
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


__all__ = ["CityPlaybook", "Venue", "SourceRegistry", "RawPage", "Activity"]
