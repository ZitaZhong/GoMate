"""DD-18 §8 BFF 房间路由：活动房间与市内多人协作全部 15 个端点。

SSE 事件对齐前端契约（web-v2/lib/sse.ts）：room_state / progress / activity_candidates /
interrupt / gathering / member_routes / itinerary / revision_classified / needs_confirmation /
itinerary_updated / done / error。房间级串行锁复用 plan_lock（键前缀 room-）。
"""
from __future__ import annotations

import json
import re
from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from sse_starlette.sse import EventSourceResponse

from ..db import get_session
from ..enums import RevisionType, RoomStatus
from ..infra.redis_client import plan_lock
from ..models import Room, RoomMember
from ..orchestration import RoomPlannerService
from ..rooms import (
    InvalidTransition,
    apply_revision,
    classify_revision,
    confirm_theme,
    create_room,
    current_itinerary,
    get_room_by_invite,
    join_room,
    member_dicts,
    room_summary,
    save_itinerary_version,
    share_payload,
    spin_wheel,
    transition,
    undo_itinerary,
    update_member,
    vote_tally,
    vote_theme,
)

router = APIRouter(tags=["rooms"])

_room_planner: RoomPlannerService | None = None

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def get_room_planner() -> RoomPlannerService:
    """惰性单例：优先 Postgres checkpointer（跨进程恢复），失败回退内存。"""
    global _room_planner
    if _room_planner is None:
        try:
            from ..orchestration import make_postgres_checkpointer

            _room_planner = RoomPlannerService(make_postgres_checkpointer())
        except Exception:
            _room_planner = RoomPlannerService()
    return _room_planner


# ---------------- Body 模型 ----------------
class CreateRoomBody(BaseModel):
    activity_date: date
    city: str = Field(default="上海", max_length=50)
    time_window: dict | None = None  # {"earliest": "14:00", "latest": "21:00"}
    budget_range: dict | None = None  # {"min": 0, "max": 200, "currency": "CNY"}
    creator_nickname: str = Field(default="发起人", max_length=50)

    @field_validator("time_window")
    @classmethod
    def window_must_be_hhmm(cls, v: dict | None) -> dict | None:
        if v:
            for key in ("earliest", "latest"):
                t = v.get(key)
                if t and not _HHMM.match(str(t)):
                    raise ValueError(f"time_window.{key} 必须是 HH:MM 格式")
        return v


class JoinRoomBody(BaseModel):
    nickname: str = Field(min_length=1, max_length=50)


class UpdateMemberBody(BaseModel):
    member_token: str
    origin_name: str | None = Field(default=None, max_length=200)  # 商圈/地铁站级
    origin_lng: float | None = None
    origin_lat: float | None = None
    origin_poi_id: str | None = None
    earliest_depart: str | None = None  # "14:00"
    latest_end: str | None = None  # "21:00"
    budget: int | None = Field(default=None, ge=0)  # 人均预算（分）
    interests: list[str] | None = Field(default=None, max_length=20)
    hard_constraints: list[str] | None = Field(default=None, max_length=20)
    negative_prefs: list[str] | None = Field(default=None, max_length=20)
    transport_pref: str | None = None  # walk|transit|drive|any
    note: str | None = Field(default=None, max_length=500)

    @field_validator("earliest_depart", "latest_end")
    @classmethod
    def time_must_be_hhmm(cls, v: str | None) -> str | None:
        if v and not _HHMM.match(v):
            raise ValueError("时间必须是 HH:MM 格式")
        return v

    @field_validator("transport_pref")
    @classmethod
    def pref_must_be_known(cls, v: str | None) -> str | None:
        if v and v not in ("walk", "transit", "drive", "any"):
            raise ValueError("transport_pref 仅支持 walk/transit/drive/any")
        return v


class VoteBody(BaseModel):
    member_token: str
    theme: str = Field(min_length=1, max_length=50)
    weight: int = Field(default=1)

    @field_validator("weight")
    @classmethod
    def weight_must_be_valid(cls, v: int) -> int:
        if v not in (1, 3, -2):
            raise ValueError("weight 仅支持 1(可接受)/3(强烈喜欢)/-2(不喜欢)")
        return v


class ConfirmThemeBody(BaseModel):
    theme: str = Field(min_length=1, max_length=50)
    method: str = Field(default="direct")  # direct|vote|ai|wheel


class SelectActivityBody(BaseModel):
    activity_id: int | str | None = None
    activity: dict | None = None


class ModifyBody(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    confirm: bool = False  # 二次确认（needs_confirmation 后带 true 重发）


# ---------------- 小工具 ----------------
def _require_room(session, room_id: int) -> Room:
    room = session.get(Room, room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="房间不存在")
    return room


def _require_member(session, room_id: int, token: str) -> RoomMember:
    m = (
        session.query(RoomMember)
        .filter_by(room_id=room_id, member_token=token)
        .one_or_none()
    )
    if m is None:
        raise HTTPException(status_code=403, detail="member_token 无效")
    return m


def _room_dict(room: Room) -> dict:
    return {
        "id": room.id,
        "status": room.status,
        "activity_date": room.activity_date.isoformat(),
        "city": room.city,
        "time_window": room.time_window,
        "budget_range": room.budget_range,
        "theme": room.theme,
        "theme_method": room.theme_method,
        "invite_code": room.invite_code,
        "created_at": room.created_at.isoformat() if room.created_at else None,
        "expire_at": room.expire_at.isoformat() if room.expire_at else None,
    }


def _sse(ev: dict) -> dict:
    return {"event": ev["event"], "data": json.dumps(ev["data"], ensure_ascii=False, default=str)}


def _graph_event_to_sse(ev: dict) -> list[dict]:
    """RoomPlanGraph 事件 → 前端契约事件（activity_candidates 等语义事件拆分）。"""
    out = [ev]
    data = ev.get("data") or {}
    if ev["event"] == "node_output" and isinstance(data, dict):
        if data.get("activity_candidates") is not None:
            out.append({"event": "activity_candidates",
                        "data": {"candidates": data["activity_candidates"][:10]}})
        if data.get("gathering") is not None:
            out.append({"event": "gathering", "data": data["gathering"]})
        if data.get("member_routes"):
            out.append({"event": "member_routes", "data": {"routes": data["member_routes"]}})
        if data.get("itinerary") is not None:
            out.append({"event": "itinerary", "data": data["itinerary"]})
    return out


# ---------------- 房间与成员 ----------------
@router.post("/rooms")
def create_room_ep(body: CreateRoomBody):
    with get_session() as s:
        room, creator = create_room(
            s, body.activity_date, city=body.city, time_window=body.time_window,
            budget_range=body.budget_range, creator_nickname=body.creator_nickname,
        )
        return {
            "room_id": room.id,
            "invite_code": room.invite_code,
            "invite_url": f"/room/join?code={room.invite_code}",
            "member_id": creator.id,
            "member_token": creator.member_token,  # 创建者自己的凭证
            "status": room.status,
        }


@router.get("/rooms/by-invite/{code}")
def room_by_invite(code: str):
    """邀请码 → 房间（前端 /room/join 用；不泄露成员 token/坐标）。"""
    with get_session() as s:
        room = get_room_by_invite(s, code)
        if room is None:
            raise HTTPException(status_code=404, detail="邀请码无效")
        return {"room": _room_dict(room)}


@router.get("/rooms/{room_id}")
def get_room_ep(room_id: int):
    with get_session() as s:
        room = _require_room(s, room_id)
        members = member_dicts(s, room.id)
        for m in members:  # 出口脱敏：坐标不外发
            m.pop("origin_coords", None)
        # 创建者是第一个成员（用于前端区分邀请页身份）
        creator_member_id = members[0]["member_id"] if members else None
        return {"room": {**_room_dict(room), "creator_member_id": creator_member_id}, "members": members}


@router.post("/rooms/{room_id}/members")
def join_room_ep(room_id: int, body: JoinRoomBody):
    with get_session() as s:
        room = _require_room(s, room_id)
        if room.status in (RoomStatus.published.value, RoomStatus.expired.value):
            raise HTTPException(status_code=409, detail="房间已发布或过期，不能加入")
        member = join_room(s, room, body.nickname)
        return {"member_id": member.id, "member_token": member.member_token}


@router.put("/rooms/{room_id}/members/{member_id}")
def update_member_ep(room_id: int, member_id: int, body: UpdateMemberBody):
    with get_session() as s:
        _require_room(s, room_id)
        member = _require_member(s, room_id, body.member_token)
        if member.id != member_id:
            raise HTTPException(status_code=403, detail="只能更新自己的信息")
        update_member(s, member, body.model_dump(exclude={"member_token"}))
        return {"ok": True, "member_id": member.id}


@router.get("/rooms/{room_id}/summary")
def room_summary_ep(room_id: int):
    with get_session() as s:
        room = _require_room(s, room_id)
        return {"room_id": room.id, "status": room.status, **room_summary(s, room)}


# ---------------- 主题选择 ----------------
@router.post("/rooms/{room_id}/theme/vote")
def theme_vote_ep(room_id: int, body: VoteBody):
    with get_session() as s:
        room = _require_room(s, room_id)
        member = _require_member(s, room_id, body.member_token)
        vote_theme(s, room, member, body.theme, body.weight)
        return {"ok": True, "tally": vote_tally(s, room)}


@router.post("/rooms/{room_id}/theme/wheel")
def theme_wheel_ep(room_id: int):
    with get_session() as s:
        room = _require_room(s, room_id)
        try:
            return spin_wheel(s, room)
        except InvalidTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/rooms/{room_id}/theme/confirm")
def theme_confirm_ep(room_id: int, body: ConfirmThemeBody):
    with get_session() as s:
        room = _require_room(s, room_id)
        try:
            confirm_theme(s, room, body.theme, body.method)
        except InvalidTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, "theme": room.theme, "status": room.status}


@router.post("/rooms/{room_id}/back-to-theme")
def back_to_theme_ep(room_id: int):
    """回退到主题选择（推荐页空结果时允许换主题）。"""
    with get_session() as s:
        room = _require_room(s, room_id)
        if room.status != RoomStatus.recommending.value:
            raise HTTPException(status_code=409, detail=f"房间状态 {room.status} 不允许回退到主题选择")
        try:
            transition(room, RoomStatus.theme_selecting.value)
            s.flush()
        except InvalidTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "status": room.status}


# ---------------- 推荐 / 选活动（RoomPlanGraph）----------------
@router.get("/rooms/{room_id}/recommend")
def recommend_stream(room_id: int):
    """SSE：collect → theme → research(深研进度) → rank → interrupt(候选)。"""
    with get_session() as s:
        room = _require_room(s, room_id)
        if room.status != RoomStatus.recommending.value:
            raise HTTPException(status_code=409, detail=f"房间状态 {room.status} 不能启动推荐")
        room_payload = {**_room_dict(room), "id": room.id}
        members = member_dicts(s, room.id)

    def events():
        yield {"event": "room_state", "data": {"status": RoomStatus.recommending.value}}
        for ev in get_room_planner().stream_recommend(room_payload, members):
            yield from _graph_event_to_sse(ev)

    def run():
        try:
            with plan_lock(f"room-{room_id}", timeout=600, blocking_timeout=1):
                try:
                    for ev in events():
                        yield _sse(ev)
                except Exception:
                    yield _sse({"event": "error", "data": {
                        "code": "STREAM_FAILED", "degraded": True,
                        "message": "推荐生成中断，请重试"}})
        except TimeoutError:
            yield _sse({"event": "error", "data": {
                "code": "ROOM_BUSY", "degraded": False,
                "message": "该房间正在生成推荐，请稍候"}})

    return EventSourceResponse(run())


@router.post("/rooms/{room_id}/select-activity")
def select_activity_ep(room_id: int, body: SelectActivityBody):
    """选定活动 → resume 续跑 gathering→itinerary→publish（同步消费，返回摘要）。"""
    with get_session() as s:
        room = _require_room(s, room_id)
        try:
            transition(room, RoomStatus.activity_selected.value)
            transition(room, RoomStatus.planning.value)
        except InvalidTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    selected = body.activity or ({"id": body.activity_id} if body.activity_id else {})
    result: dict = {"ok": True, "room_id": room_id}
    try:
        with plan_lock(f"room-{room_id}", timeout=300, blocking_timeout=2):
            for ev in get_room_planner().stream_select(room_id, selected):
                data = ev.get("data") or {}
                if ev["event"] == "error":
                    result.update({"ok": False, "error": data.get("message")})
                elif isinstance(data, dict):
                    if data.get("itinerary_version"):
                        result["itinerary_version"] = data["itinerary_version"]
                    if data.get("status"):
                        result["status"] = data["status"]
    except TimeoutError as exc:
        raise HTTPException(status_code=409, detail="房间正在规划中，请稍候") from exc
    with get_session() as s:  # publish 节点写库失败时兜底同步状态
        room = _require_room(s, room_id)
        result.setdefault("status", room.status)
    return result


@router.get("/rooms/{room_id}/routes")
def room_routes_ep(room_id: int):
    with get_session() as s:
        _require_room(s, room_id)
        it = current_itinerary(s, room_id)
    if it:
        payload = it.payload or {}
        return {"gathering": payload.get("gathering"),
                "member_routes": payload.get("member_routes") or []}
    snap = get_room_planner().get_state(room_id)
    values = getattr(snap, "values", {}) or {}
    return {"gathering": values.get("gathering"),
            "member_routes": values.get("member_routes") or []}


@router.get("/rooms/{room_id}/plan")
def room_plan_ep(room_id: int):
    with get_session() as s:
        _require_room(s, room_id)
        it = current_itinerary(s, room_id)
        if it is None:
            raise HTTPException(status_code=404, detail="行程尚未生成")
        return {"version": it.version, "itinerary": it.payload}


# ---------------- AI 修改 / 撤销 / 分享 ----------------
@router.post("/rooms/{room_id}/plan/modify")
def modify_plan_ep(room_id: int, body: ModifyBody):
    """AI 自然语言修改（SSE）：识别 → (需确认?) → 局部更新 → 存新版本。"""
    with get_session() as s:
        room = _require_room(s, room_id)
        it = current_itinerary(s, room_id)
        if it is None:
            raise HTTPException(status_code=404, detail="行程尚未生成，无法修改")
        payload = dict(it.payload)
        members = member_dicts(s, room.id)
        window = (room.time_window or {})

    def events():
        decision = classify_revision(body.message)
        yield {"event": "revision_classified", "data": decision}
        replacement = _find_replacement(room_id, decision, payload)
        budgets = [m.get("budget") for m in members if m.get("budget")]
        new_payload, changed, confirms = apply_revision(
            payload, decision, body.message, replacement=replacement,
            common_window_end=(payload.get("common_time_window") or {}).get("end")
            or window.get("latest"),
            min_member_budget=min(budgets) if budgets else None,
        )
        if confirms and not body.confirm:
            yield {"event": "needs_confirmation",
                   "data": {"reasons": confirms, "decision": decision}}
            return
        if not changed and new_payload == payload:  # 无目标节点/无实际变更 → 不空存版本
            yield {"event": "no_change",
                   "data": {"message": "行程中没有可修改的对应节点，已保持原样", "decision": decision}}
            yield {"event": "done", "data": {"version": None}}
            return
        with get_session() as s2:
            version = save_itinerary_version(s2, room_id, new_payload)
        yield {"event": "itinerary_updated",
               "data": {"version": version, "changed_nodes": changed,
                        "itinerary": new_payload}}
        yield {"event": "done", "data": {"version": version}}

    def run():
        try:
            with plan_lock(f"room-{room_id}", timeout=120, blocking_timeout=1):
                try:
                    for ev in events():
                        yield _sse(ev)
                except Exception:
                    yield _sse({"event": "error", "data": {
                        "code": "STREAM_FAILED", "degraded": True,
                        "message": "修改失败，行程保持原样"}})
        except TimeoutError:
            yield _sse({"event": "error", "data": {
                "code": "ROOM_BUSY", "degraded": False,
                "message": "该房间正在处理其它修改，请稍候"}})

    return EventSourceResponse(run())


def _find_replacement(room_id: int, decision: dict, payload: dict) -> dict | None:
    """为 replace/add 预取候选：餐饮走 DD-11 检索；活动取推荐候选中的下一个。"""
    rtype = decision.get("revision_type")
    if rtype not in (RevisionType.replace_node.value, RevisionType.add_node.value):
        return None
    target = decision.get("target_kind")
    act_node = next((n for n in payload.get("nodes") or []
                     if n.get("type") == "activity"), {})
    if target == "dining":
        loc = act_node.get("location")
        if not loc:
            return None
        try:
            from ..retrieval import RetrievalService
            dietary = [decision["keyword"]] if decision.get("keyword") else []
            picks = RetrievalService().retrieve_dining(
                (loc[0], loc[1]), "dinner", {"dietary": dietary}, top_k=1)
            if picks:
                return {"title": picks[0].name, "evidence": picks[0].evidence}
        except Exception:
            return None
        return None
    # 换活动：取图状态里的候选列表中未被选中的下一个
    try:
        snap = get_room_planner().get_state(room_id)
        cands = (getattr(snap, "values", {}) or {}).get("activity_candidates") or []
        current_title = act_node.get("title")
        for c in cands:
            if c.get("title") != current_title:
                return {"title": c.get("title"), "venue": c.get("venue"),
                        "start": c.get("start_at"), "end": c.get("end_at"),
                        "booking_url": c.get("booking_url"),
                        "location": c.get("location"), "evidence": c.get("evidence")}
    except Exception:
        return None
    return None


@router.post("/rooms/{room_id}/plan/undo")
def undo_plan_ep(room_id: int):
    with get_session() as s:
        _require_room(s, room_id)
        payload = undo_itinerary(s, room_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="没有可撤销的历史版本")
        it = current_itinerary(s, room_id)
        return {"version": it.version if it else None, "itinerary": payload}


@router.get("/rooms/{room_id}/share")
def share_ep(room_id: int):
    with get_session() as s:
        room = _require_room(s, room_id)
        return share_payload(s, room)
