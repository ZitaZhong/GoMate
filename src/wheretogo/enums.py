"""全系统共享枚举（DD-01 §4）。

Python 侧用 `str, Enum`，其 value 与 PostgreSQL ENUM 标签逐字一致，避免字符串漂移。
DB 侧的 ENUM 类型由迁移创建；应用侧统一引用本模块。
"""
from __future__ import annotations

from enum import Enum


class VerificationStatus(str, Enum):
    """证据六态（v1 §6.1）——全系统对外事实字段的可信度标签。"""

    confirmed_by_user = "confirmed_by_user"
    official_source_confirmed = "official_source_confirmed"
    public_source_observed = "public_source_observed"
    estimated = "estimated"
    unknown = "unknown"
    expired = "expired"


#: 可作为“核心可信活动”的确认态（DD-05 召回硬过滤白名单）
TRUSTED_STATUSES: tuple[VerificationStatus, ...] = (
    VerificationStatus.official_source_confirmed,
    VerificationStatus.public_source_observed,
)


class SourceType(str, Enum):
    """来源类型（v1 §5.3 可信度分级）。"""

    official_venue = "official_venue"
    culture_bureau = "culture_bureau"
    open_dataset = "open_dataset"
    search = "search"
    user_provided = "user_provided"
    editorial = "editorial"
    community = "community"
    amap = "amap"
    qweather = "qweather"
    variflight = "variflight"
    llm = "llm"


class PlanStage(str, Enum):
    explore = "explore"
    await_booking = "await_booking"
    confirm = "confirm"


class TransportMode(str, Enum):
    rail = "rail"
    air = "air"
    mixed = "mixed"


class BookingKind(str, Enum):
    train = "train"
    flight = "flight"
    hotel = "hotel"


class AvailabilityStatus(str, Enum):
    user_must_confirm = "user_must_confirm"
    likely_available = "likely_available"
    sold_out = "sold_out"
    unknown = "unknown"


class ReminderType(str, Enum):
    presale = "presale"
    activity_booking = "activity_booking"
    flight_recheck = "flight_recheck"
    pre_trip_72h = "pre_trip_72h"
    weather_24h = "weather_24h"
    doc_check = "doc_check"
    hotel_cancel_deadline = "hotel_cancel_deadline"
    activity_start = "activity_start"
    return_trip = "return_trip"


class ReminderChannel(str, Enum):
    web_push = "web_push"
    email = "email"
    ics = "ics"


class BundleVersion(str, Enum):
    explore = "explore"
    confirm = "confirm"


class SlotKind(str, Enum):
    transport = "transport"
    activity = "activity"
    meal = "meal"
    lodging = "lodging"
    buffer = "buffer"
    free = "free"


class RoomStatus(str, Enum):
    """活动房间状态机 8 态（DD-18 §3.2；TEXT 存储 + 应用侧校验，不进 PG ENUM）。"""

    draft = "DRAFT"
    collecting = "COLLECTING"
    theme_selecting = "THEME_SELECTING"
    recommending = "RECOMMENDING"
    activity_selected = "ACTIVITY_SELECTED"
    planning = "PLANNING"
    published = "PUBLISHED"
    expired = "EXPIRED"


class ThemeMethod(str, Enum):
    """主题确定方式（DD-18 §2.1）。"""

    direct = "direct"
    vote = "vote"
    ai = "ai"
    wheel = "wheel"


class RevisionType(str, Enum):
    """AI 自然语言修改类型（DD-18 §5.1）。"""

    replace_node = "replace_node"
    add_node = "add_node"
    remove_node = "remove_node"
    adjust_time = "adjust_time"
    adjust_budget = "adjust_budget"
    change_transport = "change_transport"
    change_theme = "change_theme"
    full_replan = "full_replan"


#: PostgreSQL ENUM 类型名 -> 枚举类（迁移与模型共享，保证唯一事实来源）
PG_ENUMS: dict[str, type[Enum]] = {
    "verification_status": VerificationStatus,
    "source_type": SourceType,
    "plan_stage": PlanStage,
    "transport_mode": TransportMode,
    "booking_kind": BookingKind,
    "availability_status": AvailabilityStatus,
    "reminder_type": ReminderType,
    "reminder_channel": ReminderChannel,
    "bundle_version": BundleVersion,
    "slot_kind": SlotKind,
}
