"""DD-18 房间服务：生命周期状态机 / 成员管理 / 主题选择 / 版本管理 / 分享脱敏。

状态推进集中在 `transition`（非法跳转抛 InvalidTransition → BFF 409）；
所有函数显式收 session（与 services/privacy.py 同风格），由调用方管事务。
"""
from __future__ import annotations

import secrets
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import RoomStatus, ThemeMethod
from ..models import Room, RoomItinerary, RoomMember, ThemeVote
from .algorithms import (
    candidate_themes,
    compute_common_window,
    hard_excluded_themes,
    tally_votes,
    weighted_wheel,
)

#: 合法状态转换（DD-18 §3.2）；EXPIRED 可从任意状态进入
# 注意：RECOMMENDING 允许回退到 THEME_SELECTING（推荐页空结果时换主题）
_TRANSITIONS: dict[str, set[str]] = {
    RoomStatus.draft.value: {RoomStatus.collecting.value},
    RoomStatus.collecting.value: {RoomStatus.theme_selecting.value},
    RoomStatus.theme_selecting.value: {RoomStatus.recommending.value},
    RoomStatus.recommending.value: {RoomStatus.activity_selected.value, RoomStatus.theme_selecting.value},
    RoomStatus.activity_selected.value: {RoomStatus.planning.value},
    RoomStatus.planning.value: {RoomStatus.published.value},
    RoomStatus.published.value: set(),
    RoomStatus.expired.value: set(),
}

_MAX_WHEEL_SPINS = 2  # 一次反悔（GoMate PRD §7.3.3）
_KEEP_VERSIONS = 5  # 只保留最近 5 个行程版本（DD-18 §11）


class InvalidTransition(Exception):
    """非法状态跳转（BFF 转 409）。"""


def transition(room: Room, to_status: str) -> None:
    if to_status == RoomStatus.expired.value:
        room.status = to_status
        return
    if to_status not in _TRANSITIONS.get(room.status, set()):
        raise InvalidTransition(f"房间状态 {room.status} 不能进入 {to_status}")
    room.status = to_status


# ============================ 房间与成员 ============================
def create_room(
    session: Session,
    activity_date,
    city: str = "上海",
    time_window: dict | None = None,
    budget_range: dict | None = None,
    creator_nickname: str = "发起人",
) -> tuple[Room, RoomMember]:
    """创建房间（含创建者成员）。基本信息已齐 → 直接进入 COLLECTING。"""
    expire_at = datetime.combine(
        activity_date, time(23, 59), tzinfo=timezone.utc
    ) + timedelta(days=7)  # 活动结束后 7 天过期
    room = Room(
        status=RoomStatus.collecting.value,
        activity_date=activity_date,
        city=city,
        time_window=time_window,
        budget_range=budget_range,
        creator_id=creator_nickname,
        thread_id="pending",
        invite_code=secrets.token_urlsafe(6),
        expire_at=expire_at,
    )
    session.add(room)
    session.flush()
    room.thread_id = f"room:{room.id}"
    creator = RoomMember(
        room_id=room.id, nickname=creator_nickname,
        member_token=secrets.token_urlsafe(12), is_creator=True,
    )
    session.add(creator)
    session.flush()
    return room, creator


def get_room_by_invite(session: Session, invite_code: str) -> Room | None:
    return session.scalar(select(Room).where(Room.invite_code == invite_code))


def join_room(session: Session, room: Room, nickname: str) -> RoomMember:
    """成员凭邀请码加入；发 member_token 作后续更新凭证。"""
    member = RoomMember(
        room_id=room.id, nickname=nickname, member_token=secrets.token_urlsafe(12),
    )
    session.add(member)
    session.flush()
    return member


_MEMBER_FIELDS = (
    "origin_name", "origin_poi_id", "earliest_depart", "latest_end", "budget",
    "interests", "hard_constraints", "negative_prefs", "transport_pref", "note",
)


def update_member(session: Session, member: RoomMember, fields: dict) -> RoomMember:
    """更新成员信息（member_token 已由 BFF 校验）；首次提交记 submitted_at。"""
    for key in _MEMBER_FIELDS:
        if key in fields and fields[key] is not None:
            setattr(member, key, fields[key])
    if fields.get("origin_lng") is not None and fields.get("origin_lat") is not None:
        member.origin_geo = f"SRID=4326;POINT({fields['origin_lng']} {fields['origin_lat']})"
    if member.submitted_at is None:
        member.submitted_at = datetime.now(timezone.utc)
    session.flush()
    return member


def member_dicts(session: Session, room_id: int) -> list[dict]:
    """成员信息 → 算法层 dict（含坐标；仅内部用，出口须脱敏）。"""
    from sqlalchemy import text as sqltext

    rows = session.execute(sqltext(
        "SELECT id, nickname, origin_name, earliest_depart, latest_end, budget, "
        "interests, hard_constraints, negative_prefs, transport_pref, submitted_at, "
        "ST_X(origin_geo::geometry) AS lng, ST_Y(origin_geo::geometry) AS lat "
        "FROM room_members WHERE room_id = :rid ORDER BY id"
    ), {"rid": room_id}).mappings().all()
    return [
        {
            "member_id": r["id"], "nickname": r["nickname"],
            "origin_name": r["origin_name"],
            "earliest_depart": r["earliest_depart"], "latest_end": r["latest_end"],
            "budget": r["budget"], "interests": list(r["interests"] or []),
            "hard_constraints": list(r["hard_constraints"] or []),
            "negative_prefs": list(r["negative_prefs"] or []),
            "transport_pref": r["transport_pref"],
            "submitted": r["submitted_at"] is not None,
            "origin_coords": [r["lng"], r["lat"]] if r["lng"] is not None else None,
        }
        for r in rows
    ]


def room_summary(session: Session, room: Room) -> dict:
    """聚合摘要：共同时间窗 + 偏好聚合 + 冲突检测（DD-18 §8 /summary）。"""
    members = member_dicts(session, room.id)
    window = compute_common_window(members)
    interests = sorted(set().union(*[set(m["interests"]) for m in members]) if members else set())
    negatives = sorted(set().union(*[set(m["negative_prefs"]) for m in members]) if members else set())
    hard = sorted(set().union(*[set(m["hard_constraints"]) for m in members]) if members else set())
    budgets = [m["budget"] for m in members if m.get("budget")]
    conflicts = [
        {"theme": t, "reason": "部分成员喜欢但另一些成员不接受"}
        for t in interests if t in negatives
    ]
    return {
        "members": [
            {k: m[k] for k in ("member_id", "nickname", "origin_name", "earliest_depart",
                               "latest_end", "interests", "submitted")}
            for m in members  # 摘要不出坐标/预算明细（脱敏）
        ],
        "submitted_count": sum(1 for m in members if m["submitted"]),
        "common_window": window,
        "interests": interests,
        "negative_prefs": negatives,
        "hard_constraints": hard,
        "budget_min": min(budgets) if budgets else None,
        "conflicts": conflicts,
        "theme_candidates": candidate_themes(members),
    }


# ============================ 主题选择 ============================
def vote_theme(session: Session, room: Room, member: RoomMember,
               theme: str, weight: int = 1) -> None:
    """提交/更新投票（UNIQUE(room,member,theme) → upsert 语义）。"""
    existing = session.scalar(select(ThemeVote).where(
        ThemeVote.room_id == room.id, ThemeVote.member_id == member.id,
        ThemeVote.theme == theme))
    if existing:
        existing.weight = weight
    else:
        session.add(ThemeVote(room_id=room.id, member_id=member.id,
                              theme=theme, weight=weight))
    session.flush()


def vote_tally(session: Session, room: Room) -> list[dict]:
    votes = session.scalars(select(ThemeVote).where(ThemeVote.room_id == room.id)).all()
    return tally_votes([{"theme": v.theme, "weight": v.weight} for v in votes])


def spin_wheel(session: Session, room: Room, weather: dict | None = None) -> dict:
    """转盘（支持一次反悔=最多 2 次）；硬约束排除 + 偏好加权（DD-18 §4.2）。"""
    if room.wheel_spins >= _MAX_WHEEL_SPINS:
        raise InvalidTransition("转盘次数已用完（含一次反悔），请直接选择主题")
    members = member_dicts(session, room.id)
    themes = candidate_themes(members)
    excluded = hard_excluded_themes(members)
    theme, weights = weighted_wheel(themes, members, weather=weather, hard_excluded=excluded)
    room.wheel_spins += 1
    session.flush()
    return {"theme": theme, "weights": weights,
            "spins_left": _MAX_WHEEL_SPINS - room.wheel_spins,
            "excluded": sorted(excluded)}


def confirm_theme(session: Session, room: Room, theme: str, method: str) -> None:
    """确认主题 → THEME_SELECTING/COLLECTING 收敛为 RECOMMENDING。"""
    ThemeMethod(method)  # 非法 method 抛 ValueError → BFF 422
    if room.status == RoomStatus.collecting.value:  # 允许跳过显式 THEME_SELECTING 阶段
        transition(room, RoomStatus.theme_selecting.value)
    transition(room, RoomStatus.recommending.value)
    room.theme = theme
    room.theme_method = method
    session.flush()


# ============================ 行程版本管理（DD-18 §6）============================
def save_itinerary_version(session: Session, room_id: int, payload: dict) -> int:
    """保存新版本：旧版本取消 is_current，超过 5 版清理最老的。"""
    current = session.scalar(select(RoomItinerary).where(
        RoomItinerary.room_id == room_id, RoomItinerary.is_current.is_(True)))
    if current:
        current.is_current = False
    max_ver = session.scalar(
        select(RoomItinerary.version).where(RoomItinerary.room_id == room_id)
        .order_by(RoomItinerary.version.desc()).limit(1)) or 0
    new_ver = max_ver + 1
    session.add(RoomItinerary(room_id=room_id, version=new_ver,
                              payload=payload, is_current=True))
    session.flush()
    _cleanup_old_versions(session, room_id, keep=_KEEP_VERSIONS)
    return new_ver


def _cleanup_old_versions(session: Session, room_id: int, keep: int) -> None:
    rows = session.scalars(
        select(RoomItinerary).where(RoomItinerary.room_id == room_id)
        .order_by(RoomItinerary.version.desc())).all()
    for old in rows[keep:]:
        session.delete(old)
    session.flush()


def current_itinerary(session: Session, room_id: int) -> RoomItinerary | None:
    return session.scalar(select(RoomItinerary).where(
        RoomItinerary.room_id == room_id, RoomItinerary.is_current.is_(True)))


def undo_itinerary(session: Session, room_id: int) -> dict | None:
    """撤销：回退到上一个版本（GoMate PRD §7.8.5）。无上一版 → None。"""
    prev = session.scalar(
        select(RoomItinerary).where(
            RoomItinerary.room_id == room_id, RoomItinerary.is_current.is_(False))
        .order_by(RoomItinerary.version.desc()).limit(1))
    if not prev:
        return None
    cur = current_itinerary(session, room_id)
    if cur:
        cur.is_current = False
        # 已撤销的版本不应再被下一次 undo 选中 → 物理删除（保持"回退链"语义）
        session.delete(cur)
    prev.is_current = True
    session.flush()
    return prev.payload


# ============================ 分享脱敏（DD-18 §10）============================
_SENSITIVE_KEYS = {"origin_coords", "origin_geo", "origin_name", "member_token",
                   "budget", "lng", "lat", "coords_precise", "phone", "contact"}


def share_payload(session: Session, room: Room) -> dict:
    """分享卡数据：不含精确出发地/经纬度/联系方式/个人预算（DD-18 §10）。"""
    it = current_itinerary(session, room.id)
    payload = _strip_sensitive(dict(it.payload)) if it else None
    members = [{"nickname": m["nickname"]} for m in member_dicts(session, room.id)]
    return {
        "room_id": room.id,
        "city": room.city,
        "activity_date": room.activity_date.isoformat(),
        "theme": room.theme,
        "status": room.status,
        "members": members,
        "itinerary": payload,
    }


def _strip_sensitive(obj):
    """递归剔除敏感键；member_departures 仅保留昵称+时间。"""
    if isinstance(obj, dict):
        return {k: _strip_sensitive(v) for k, v in obj.items() if k not in _SENSITIVE_KEYS}
    if isinstance(obj, list):
        return [_strip_sensitive(x) for x in obj]
    return obj
