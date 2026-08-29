"""BFF：REST + SSE（DD-02 §11 接口契约与事件 schema）。

事件流对齐 AG-UI 思路：前端只需按 event 类型渲染卡片（每字段带 evidence → 六态可视）。
Planner 默认用 Postgres checkpointer（跨天/跨进程恢复）；不可用时回退内存。
"""
from __future__ import annotations

import json
import logging
import math
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from sse_starlette.sse import EventSourceResponse
from fastapi.middleware.cors import CORSMiddleware

from ..config import get_settings
from ..agent.persist import (
    agent_results_from_bundle as _shared_agent_results_from_bundle,
    compact_agent_activity as _shared_compact_agent_activity,
    persist_stream_event as _shared_persist_stream_event,
)
from ..copilot import handle_turn
from ..db import get_session
from ..enums import BookingKind
from ..domain.compose import build_ics, build_ics_fallback
from ..domain.constraints import aggregate_party, missing_slots
from ..infra.redis_client import plan_lock
from ..models import Booking, PartyConstraint, Plan, PlanMember, TripBundle
from ..orchestration import PlannerService
from .rooms import router as rooms_router

app = FastAPI(title="周末去哪儿 BFF", version="0.1.0")
logger = logging.getLogger(__name__)

# v4 Agent API（Turn/Run/Workspace）；旧端点保留为兼容层
from .agent_api import router as _agent_router  # noqa: E402

app.include_router(_agent_router)

# web-v2 前端本地开发直连（Next dev rewrites 代理对慢请求/SSE 不可靠，DD-19 联调结论）；
# 3000 被占时 Next 会自动顺延端口，故白名单覆盖 3000/3001；可用 WTG_CORS_EXTRA_ORIGINS 追加。
# 生产环境由网关/Ingress 统一域名时收紧 allow_origins。
import os as _os

_dev_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3001",
] + [o for o in _os.environ.get("WTG_CORS_EXTRA_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_dev_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(rooms_router)  # DD-18 活动房间与市内多人协作

_planner: PlannerService | None = None


def get_planner() -> PlannerService:
    """惰性单例：优先 Postgres checkpointer（持久化恢复），失败回退内存。"""
    global _planner
    if _planner is None:
        try:
            from ..orchestration import make_postgres_checkpointer

            _planner = PlannerService(make_postgres_checkpointer())
        except Exception:
            _planner = PlannerService()
    return _planner


# ---------------- 请求体 ----------------
class CreatePlanBody(BaseModel):
    constraints: dict = Field(default_factory=dict)
    party: list[dict] = Field(default_factory=list, max_length=20)
    organizer_user_id: int | None = None  # D2：老用户注入长期偏好作缺省


class ResumeBody(BaseModel):
    bookings: list[dict] = Field(default_factory=list, max_length=20)


class ReplanBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    from_node: Literal[
        "parse", "discover", "research", "reflect", "transport", "await_booking",
        "hotel", "mobility", "dining", "weather", "timeline", "validate", "compose",
    ] = "dining"


class ReviseBody(BaseModel):
    values: dict
    from_node: Literal[
        "parse", "discover", "research", "reflect", "transport", "await_booking",
        "hotel", "mobility", "dining", "weather", "timeline", "validate", "compose",
    ] = "timeline"


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    memory_ctx: dict = Field(default_factory=dict)

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message 不能为空")
        return value


class ImportBody(BaseModel):
    # manual = 未知类型，由 run_extract 从文本自动识别（前端回填面板/扩展默认入口）
    kind: Literal["train", "flight", "hotel", "manual"]
    input_kind: Literal["text", "image", "link", "manual"] = "manual"
    extracted: dict = Field(default_factory=dict)
    raw: str | None = Field(default=None, max_length=20_000)
    token: str | None = Field(default=None, max_length=2_000)


class InviteBody(BaseModel):
    count: int = Field(default=1, ge=1, le=8)


class MemberConstraintsBody(BaseModel):
    origin_area: str | None = Field(default=None, max_length=200)  # 商圈级脱敏
    earliest_depart: str | None = None
    latest_return: str | None = None
    budget_band: dict | None = None
    prefer_flight: bool | None = None
    accept_flight: bool | None = None  # 兼容领域聚合字段名
    accept_night_train: bool | None = None
    interests: list[str] | None = Field(default=None, max_length=20)
    dietary: list[str] | None = Field(default=None, max_length=20)

    @field_validator("earliest_depart", "latest_return")
    @classmethod
    def datetime_must_be_iso8601(cls, value: str | None) -> str | None:
        if value:
            try:
                datetime.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("时间必须是 ISO 8601 格式") from exc
        return value

    @field_validator("budget_band")
    @classmethod
    def budget_must_be_numeric_and_ordered(cls, value: dict | None) -> dict | None:
        if value is None:
            return None
        allowed = {"min", "max"}
        if set(value) - allowed:
            raise ValueError("budget_band 仅支持 min/max")
        for key in allowed:
            amount = value.get(key)
            if amount is not None and (
                isinstance(amount, bool)
                or not isinstance(amount, (int, float))
                or not math.isfinite(amount)
                or amount < 0
            ):
                raise ValueError(f"budget_band.{key} 必须是非负数")
        if value.get("min") is not None and value.get("max") is not None:
            if value["min"] > value["max"]:
                raise ValueError("budget_band.min 不能大于 max")
        return value

    @model_validator(mode="after")
    def departure_must_precede_return(self):
        if self.earliest_depart and self.latest_return:
            departure = datetime.fromisoformat(self.earliest_depart)
            latest_return = datetime.fromisoformat(self.latest_return)
            if (departure.tzinfo is None) != (latest_return.tzinfo is None):
                raise ValueError("出发和返回时间必须使用一致的时区格式")
            if departure > latest_return:
                raise ValueError("最早出发时间不能晚于最晚返回时间")
        return self


# ---------------- 持久化辅助 ----------------
def _load_constraints(plan_id: str) -> dict:
    with get_session() as s:
        p = s.get(Plan, int(plan_id))
        return dict(p.constraints) if p else {}


def _load_thread_id(plan_id: str) -> str:
    """读取当前规划版本的 checkpoint thread_id。"""
    with get_session() as s:
        p = s.get(Plan, int(plan_id))
        return (p.thread_id if p and p.thread_id else f"plan:{plan_id}")


def _load_conversation(plan_id: str) -> list[dict]:
    with get_session() as s:
        p = s.get(Plan, int(plan_id))
        return list(p.conversation or []) if p else []


def _compact_agent_activity(item: dict) -> dict:
    # v4 提取到 agent/persist.py（BFF 与 Worker 共用）；此处薄委托，行为不变。
    return _shared_compact_agent_activity(item)


def _agent_results_from_bundle(payload: dict | None) -> dict:
    return _shared_agent_results_from_bundle(payload)


def _booking_key(b: dict) -> tuple:
    ex = b.get("extracted") or {}
    return (b.get("kind"), ex.get("train_no") or ex.get("flight_no") or ex.get("name"))


def _load_bookings(plan_id: str) -> list[dict]:
    """读回已确认的回填（P1-6b：import 落库后 resume 可持久加载）。"""
    with get_session() as s:
        rows = s.query(Booking).filter_by(plan_id=int(plan_id), confirmed=True).all()
        return [{"kind": b.kind.value if hasattr(b.kind, "value") else b.kind,
                 "input_kind": b.input_kind, "extracted": b.extracted or {},
                 "evidence": b.evidence, "confirmed": b.confirmed} for b in rows]


def _merge_bookings(plan_id: str, body_bookings: list[dict]) -> list[dict]:
    """合并 DB 已确认回填 + 请求体回填（去重），使 import 的回填在 resume 时生效。"""
    persisted = _load_bookings(plan_id)
    combined = list(persisted)
    seen = {_booking_key(b) for b in persisted}
    for b in (body_bookings or []):
        k = _booking_key(b)
        if k not in seen:
            combined.append(b)
            seen.add(k)
    return combined


def _persist_booking(plan_id: str, confirmed: dict, raw: str | None) -> None:
    """确认态回填落库（P1-6b：此前 import 从不写 bookings 表）。"""
    try:
        kind = BookingKind(confirmed.get("kind"))
    except ValueError:
        return  # kind 非 train/flight/hotel（如 manual 未解析）→ 不落库
    with get_session() as s:
        s.add(Booking(
            plan_id=int(plan_id), kind=kind, raw_input=raw,
            input_kind=confirmed.get("input_kind"), extracted=confirmed.get("extracted"),
            evidence=confirmed.get("evidence") or {}, confirmed=bool(confirmed.get("confirmed")),
            confirmed_at=datetime.now(timezone.utc) if confirmed.get("confirmed") else None,
        ))
        s.commit()


def _require_plan(plan_id: str) -> None:
    """校验 plan 存在；不存在→404（修 P2-4：未知 plan 返回 200）。"""
    if not (plan_id or "").isdigit():
        raise HTTPException(status_code=404, detail="plan_id 无效")
    with get_session() as s:
        if not s.get(Plan, int(plan_id)):
            raise HTTPException(status_code=404, detail="plan 不存在")


def _persist_event(plan_id: str, ev: dict) -> None:
    """把探索版/确认版 bundle 落库（DD-13 trip_bundles），并推进 plan.stage。

    v4 提取到 agent/persist.py（Worker 共用同一段逻辑）；此处薄委托，行为不变。
    """
    _shared_persist_stream_event(plan_id, ev)


def _sse(ev: dict) -> dict:
    return {"event": ev["event"], "data": json.dumps(ev["data"], ensure_ascii=False, default=str)}


def _run_stream(plan_id: str, events):
    try:
        with plan_lock(plan_id, timeout=900, blocking_timeout=1):
            try:
                stream = events() if callable(events) else events
                for ev in stream:
                    _persist_event(plan_id, ev)
                    yield _sse(ev)
            except Exception:
                yield _sse({
                    "event": "error",
                    "data": {
                        "code": "STREAM_FAILED",
                        "message": "方案生成中断，请重试；已保留此前可用结果。",
                        "degraded": True,
                    },
                })
    except TimeoutError:
        yield _sse({
            "event": "error",
            "data": {
                "code": "PLAN_BUSY",
                "message": "该方案正在生成中，请等待当前研究完成后再试。",
                "degraded": False,
            },
        })


# ---------------- 路由（DD-02 §11）----------------
@app.get("/health")
def health():
    return {"ok": True}


@app.post("/plans")
def create_plan(body: CreatePlanBody):
    constraints = dict(body.constraints or {})
    # D2 记忆注入：老用户的稳定偏好作缺省（不覆盖已填字段）
    if body.organizer_user_id:
        try:
            from ..memory import load_memory
            mem = load_memory(body.organizer_user_id)
            for k in ("interests", "dietary"):
                if not constraints.get(k) and (mem.get("structured") or {}).get(k):
                    constraints[k] = mem["structured"][k]
        except Exception:
            pass
    with get_session() as s:
        p = Plan(stage="explore", thread_id=f"pending-{uuid4()}", constraints=constraints)
        s.add(p)
        s.flush()
        p.thread_id = f"plan:{p.id}"
        plan_id = str(p.id)
    return {"plan_id": plan_id, "stream": f"/plans/{plan_id}/stream"}


@app.get("/plans/{plan_id}/agent-state")
def agent_state(plan_id: str):
    """Restore the durable travel-assistant workspace for the chat UI."""
    _require_plan(plan_id)
    with get_session() as s:
        plan = s.get(Plan, int(plan_id))
        bundle_row = (
            s.query(TripBundle)
            .filter_by(plan_id=int(plan_id), version="explore")
            .order_by(TripBundle.created_at.desc())
            .first()
        )
        conversation = [
            {
                "role": turn.get("role"),
                "content": turn.get("content"),
                "intent": turn.get("intent"),
            }
            for turn in (plan.conversation or [])
            if turn.get("role") in {"user", "assistant"}
            and str(turn.get("content") or "").strip()
        ]
        return {
            "plan_id": plan_id,
            "stage": plan.stage,
            "constraints": dict(plan.constraints or {}),
            "conversation": conversation,
            "explore_bundle": (
                dict(bundle_row.payload or {}) if bundle_row else None
            ),
        }


@app.get("/plans/{plan_id}/stream")
def stream_plan(plan_id: str):
    _require_plan(plan_id)
    def events():
        planner = get_planner()
        kwargs = {"thread_id": _load_thread_id(plan_id)}
        try:
            return planner.stream_start(
                plan_id,
                _load_constraints(plan_id),
                conversation=_load_conversation(plan_id),
                **kwargs,
            )
        except TypeError:
            # Compatibility for lightweight test/extension planner adapters.
            return planner.stream_start(
                plan_id,
                _load_constraints(plan_id),
                **kwargs,
            )
    return EventSourceResponse(_run_stream(plan_id, events))


@app.post("/plans/{plan_id}/resume")
def resume_plan(plan_id: str, body: ResumeBody):
    _require_plan(plan_id)
    def events():
        return get_planner().stream_resume(
            plan_id,
            _merge_bookings(plan_id, body.bookings),
            thread_id=_load_thread_id(plan_id),
        )
    return EventSourceResponse(_run_stream(plan_id, events))


@app.post("/plans/{plan_id}/replan")
def replan_plan(plan_id: str, body: ReplanBody):
    _require_plan(plan_id)
    def events():
        return get_planner().stream_replan(
            plan_id, body.reason, body.from_node, thread_id=_load_thread_id(plan_id)
        )
    return EventSourceResponse(_run_stream(plan_id, events))


@app.post("/plans/{plan_id}/research-more")
def research_more(plan_id: str):
    """研究迭代续流：从 reflect 节点继续执行图（对标 LangGraph Command(resume)）。

    前端在收到 chat 返回 auto_stream=true 后调用此端点。
    update_state 已由 chat handler 完成，此处仅继续执行图得到新结果。
    """
    _require_plan(plan_id)
    snap = get_planner().get_state(plan_id, thread_id=_load_thread_id(plan_id))
    values = getattr(snap, "values", {}) or {}
    if not (
        values.get("research_feedback")
        or values.get("research_active_feedback")
    ):
        raise HTTPException(status_code=409, detail="当前没有待处理的深度研究反馈")

    def events():
        return get_planner().stream_research_more(
            plan_id, thread_id=_load_thread_id(plan_id)
        )
    return EventSourceResponse(_run_stream(plan_id, events))


@app.post("/plans/{plan_id}/revise")
def revise_plan(plan_id: str, body: ReviseBody):
    _require_plan(plan_id)
    try:
        with plan_lock(plan_id):  # DD-02 §9：同一 plan 串行（避免并发写 checkpoint）
            out = get_planner().revise(
                plan_id, body.values, body.from_node, thread_id=_load_thread_id(plan_id)
            )
    except TimeoutError as exc:
        raise HTTPException(status_code=409, detail="该方案正在生成中，请稍后再修改。") from exc
    return JSONResponse({"ok": True, "stage": out["state"].get("stage")})


@app.post("/plans/{plan_id}/chat")
def chat_turn(plan_id: str, body: ChatBody):
    if not plan_id.isdigit() and plan_id != "new":
        raise HTTPException(status_code=404, detail="plan 不存在")
    if not plan_id.isdigit():
        return _chat_turn_impl(plan_id, body)
    try:
        with plan_lock(
            plan_id,
            timeout=get_settings().chat_plan_lock_timeout_s,
            blocking_timeout=1,
        ):
            return _chat_turn_impl(plan_id, body)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=409,
            detail="该方案正在生成中，请等待当前研究完成后再修改。",
        ) from exc


def _chat_turn_impl(plan_id: str, body: ChatBody):
    """DD-15 对话式 Copilot：从既有约束(memory_ctx)出发，抽约束/回答/回填 + 落库 patch。

    不反复追问已填信息：memory_ctx 取自 plans.constraints（BFF 自己读库，不依赖前端传入）。
    """
    with get_session() as s:
        if plan_id.isdigit():
            p = s.get(Plan, int(plan_id))
            if p is None:  # 修 P2：不存在的数字 plan → 404（"new"/非数字保留 chat-first 预规划）
                raise HTTPException(status_code=404, detail="plan 不存在")
        else:
            p = None
        memory_ctx = dict(p.constraints) if p else dict(body.memory_ctx or {})
        conversation = list((p.conversation or []) if p else [])
        pending_from_history: list[dict] = []
        for turn in reversed(conversation):
            if turn.get("role") == "assistant":
                pending_from_history = list(turn.get("pending_clarify") or [])
                break
        latest_results: dict = {}
        latest_bundle_payload: dict = {}
        has_research_result = False
        if p is not None:
            bundle_row = (
                s.query(TripBundle)
                .filter_by(plan_id=int(p.id), version="explore")
                .order_by(TripBundle.created_at.desc())
                .first()
            )
            if bundle_row:
                latest_bundle_payload = dict(bundle_row.payload or {})
                latest_results = _agent_results_from_bundle(bundle_row.payload)
                has_research_result = bool(
                    latest_results.get("activities")
                    or latest_results.get("research_context")
                )
            for turn in reversed(conversation):
                if turn.get("intent") == "research_result":
                    persisted_results = {
                        "activities": list(turn.get("cards") or [])[:30],
                        "itinerary_draft": list(
                            turn.get("itinerary_draft") or []
                        )[:12],
                        "research_context": dict(turn.get("research_context") or {}),
                        "plan_ledger": dict(turn.get("plan_ledger") or {}),
                        "plan_delta": dict(turn.get("plan_delta") or {}),
                        "assistant_response": turn.get("content"),
                    }
                    latest_results = {
                        **persisted_results,
                        **{
                            key: value
                            for key, value in latest_results.items()
                            if value not in (None, [], {})
                        },
                    }
                    has_research_result = bool(
                        latest_results.get("activities")
                        or latest_results.get("research_context")
                    )
                    break
            try:
                snapshot = get_planner().get_state(str(p.id), thread_id=p.thread_id)
                values = getattr(snapshot, "values", {}) or {}
                activities = list(values.get("activities") or [])[:30]
                checkpoint_results = {
                    "activities": [
                        _compact_agent_activity(item)
                        for item in activities
                    ],
                    "itinerary_draft": list(
                        values.get("itinerary_draft") or []
                    )[:12],
                    "research_outcome": values.get("research_outcome"),
                    "research_quality": values.get("research_quality") or {},
                    "research_selection": values.get("research_selection") or {},
                    "plan_ledger": values.get("plan_ledger") or {},
                    "plan_delta": values.get("plan_delta") or {},
                    "research_artifacts": list(
                        values.get("research_artifacts") or []
                    )[-4:],
                    "assistant_response": values.get("assistant_response"),
                }
                latest_results = {
                    **latest_results,
                    **{
                        key: value
                        for key, value in checkpoint_results.items()
                        if value not in (None, [], {})
                    },
                }
                has_research_result = bool(
                    latest_results.get("activities")
                    or latest_results.get("research_context")
                    or values.get("constraints")
                )
            except Exception:
                pass
        try:
            decision = handle_turn(
                plan_id, body.message, memory_ctx=memory_ctx, session=s,
                city_code=memory_ctx.get("target_city_code"),
                conversation=conversation,
                stage=(p.stage if p else "explore"),
                pending_clarify_ctx=pending_from_history,
                latest_results=latest_results,
            )
            decision_acts = set(decision.get("acts") or [])
            local_recompose = bool(
                has_research_result
                and "recompose_plan" in decision_acts
                and "research_more" not in decision_acts
            )
            if (
                local_recompose
                and not decision.get("itinerary_draft")
                and latest_results.get("itinerary_draft")
            ):
                decision["itinerary_draft"] = list(
                    latest_results["itinerary_draft"]
                )
            # 落库抽取到的约束；chat-first 首条带约束的消息自动建 plan（跨轮持久，修多轮记忆丢失）
            patch = dict(decision.get("constraints_patch") or {})
            research_command = next(
                (
                    command for command in (decision.get("commands") or [])
                    if command.get("type") == "research_more"
                ),
                None,
            )
            research_feedback = patch.pop("__research_feedback", None)
            if not research_feedback and research_command:
                research_feedback = (research_command.get("payload") or {}).get("feedback")
            prepared_research = False
            revision_mode = "alternative"
            requires_full_replan = False
            if patch or research_feedback:
                # 研究迭代反馈：__research_feedback 不写入约束，而是注入图状态触发回环
                if p is not None and patch:
                    current = dict(p.constraints or {})
                    old_requirements = {
                        str(value).strip()
                        for value in (current.get("experience_requirements") or [])
                        if str(value).strip()
                    }
                    new_requirements = {
                        str(value).strip()
                        for value in (
                            patch.get(
                                "experience_requirements",
                                current.get("experience_requirements") or [],
                            )
                            or []
                        )
                        if str(value).strip()
                    }
                    if (
                        old_requirements
                        and old_requirements < new_requirements
                    ):
                        revision_mode = "extend"
                    elif (
                        "experience_requirements" in patch
                        or "research_subgoals" in patch
                    ):
                        revision_mode = "replace"
                    if patch.get("soft_preferences"):
                        patch["soft_preferences"] = list(dict.fromkeys(
                            (current.get("soft_preferences") or [])
                            + list(patch["soft_preferences"])
                        ))
                    patch = {
                        key: value
                        for key, value in patch.items()
                        if current.get(key) != value
                    }
                    # A research-only resume starts at the existing graph's
                    # research node and intentionally retains destination
                    # discovery.  Therefore any route/time identity change
                    # must create a new graph thread and rerun discovery.
                    requires_full_replan = bool(
                        set(patch)
                        & {
                            "origins",
                            "target_city_code",
                            "target_city_name",
                            "weekend_start",
                            "weekend_end",
                            "earliest_depart",
                            "latest_return",
                        }
                    )
                    if (
                        not patch
                        and not research_feedback
                        and not (
                            {"answer_info", "recompose_plan"}
                            & decision_acts
                        )
                    ):
                        decision["reply"] = "这些偏好已经生效，当前方案无需重复重算。"
                decision["constraints_patch"] = patch or None
                if p is None and patch:  # 常规约束补丁：无 plan 时自动建
                    p = Plan(stage="explore", thread_id=f"pending-{uuid4()}", constraints={})
                    s.add(p)
                    s.flush()
                    p.thread_id = f"plan:{p.id}"
                if patch and p:
                    merged_constraints = {**(p.constraints or {}), **patch}
                    # query 是派生字段；兴趣/软偏好变化后必须失效，防止沿用旧缓存键。
                    if any(key in patch for key in (
                        "interests", "soft_preferences", "experience_requirements",
                        "research_goal", "acceptance_criteria",
                        "research_subgoals",
                        "budget_band", "dietary",
                        "target_city_code", "weekend_start", "weekend_end",
                    )):
                        merged_constraints.pop("query", None)
                    p.constraints = merged_constraints
                    s.commit()
                # 研究迭代：通过 update_state 注入 research_feedback 到图状态
                if (
                    research_feedback
                    and p
                    and has_research_result
                    and not requires_full_replan
                ):
                    try:
                        planner = get_planner()
                        snap = planner.get_state(str(p.id), thread_id=p.thread_id)
                        values = getattr(snap, "values", {}) or {}
                        if values.get("constraints"):
                            prepare_kwargs = {
                                "constraints": dict(p.constraints or {}),
                                "thread_id": p.thread_id,
                                "conversation": [
                                    *conversation,
                                    {"role": "user", "content": body.message},
                                    {
                                        "role": "assistant",
                                        "content": decision.get("reply") or "",
                                        "intent": decision.get("intent"),
                                    },
                                ],
                                "plan_ledger": dict(
                                    latest_results.get("plan_ledger") or {}
                                ),
                                "itinerary_draft": list(
                                    latest_results.get("itinerary_draft") or []
                                ),
                            }
                            try:
                                planner.prepare_research_more(
                                    str(p.id),
                                    research_feedback,
                                    revision_mode=revision_mode,
                                    **prepare_kwargs,
                                )
                            except TypeError:
                                legacy_kwargs = {
                                    key: value
                                    for key, value in prepare_kwargs.items()
                                    if key not in {
                                        "conversation",
                                        "plan_ledger",
                                        "itinerary_draft",
                                    }
                                }
                                planner.prepare_research_more(
                                    str(p.id),
                                    research_feedback,
                                    **legacy_kwargs,
                                )
                            prepared_research = True
                    except Exception:
                        prepared_research = False
                    if not prepared_research:
                        decision["restart_stream"] = not missing_slots(dict(p.constraints or {}))
                        if decision["restart_stream"]:
                            decision["reply"] = (
                                "当前还没有可迭代的上一轮结果，"
                                "先按现有偏好生成首轮方案。"
                            )
                    decision["auto_stream"] = prepared_research
                    if not decision.get("reply"):
                        decision["reply"] = "好的，我再帮你找找其他的…"
                elif research_feedback:
                    decision["auto_stream"] = False
                    if p and not missing_slots(dict(p.constraints or {})):
                        decision["restart_stream"] = True
                        decision["reply"] = (
                            "当前还没有可迭代的上一轮结果，"
                            "先按现有偏好生成首轮方案。"
                        )
            if p is not None:
                decision["plan_id"] = str(p.id)
                decision["constraints"] = dict(p.constraints)
                decision["conversation_id"] = str(p.id)
                decision["plan_revision"] = p.thread_id
            else:
                decision["constraints"] = memory_ctx
            # 约束齐备 → 可自动生成（前端据此触发流式）
            decision["ready_to_plan"] = bool(p) and not missing_slots(
                dict((p.constraints if p else {}) or {})
            )
            # 任何修改了约束 + ready 的场景都重置图（不只是 deep_research）；
            # design_itinerary 例外：路线卡就是本轮答案，不附带全量重跑
            if (
                patch
                and p
                and decision.get("ready_to_plan")
                and not prepared_research
                and not local_recompose
                and decision.get("intent") != "design_itinerary"
            ):
                import time as _t
                p.stage = "explore"
                p.thread_id = f"plan:{p.id}:r{_t.time_ns()}"  # 新线程，用新约束重新跑图
                s.commit()
                decision["plan_revision"] = p.thread_id
                # 约束变化要从新线程重跑完整规划图。它不同于 deep_research
                # 的 reflect 续流，前端应调用 /stream。
                decision["restart_stream"] = True
                if not decision.get("reply"):
                    decision["reply"] = "好的，正在为你重新调研..."
            # 命令只描述下一步，不在解释层伪装成已经执行。前端可按 next_run 统一调度。
            if p is not None:
                if decision.get("auto_stream"):
                    decision["next_run"] = {
                        "type": "research_more",
                        "endpoint": f"/plans/{p.id}/research-more",
                    }
                elif decision.get("restart_stream"):
                    decision["next_run"] = {
                        "type": "stream",
                        "endpoint": f"/plans/{p.id}/stream",
                    }
                elif any(
                    command.get("type") == "request_weather_replan"
                    for command in (decision.get("commands") or [])
                ):
                    decision["next_run"] = {
                        "type": "replan",
                        "endpoint": f"/plans/{p.id}/replan",
                        "body": {"reason": body.message, "from_node": "weather"},
                    }
            # plans.conversation 是对话兜底主存：保存语义摘要和待澄清状态，
            # 不保存内部异常、完整 checkpoint 或未确认订单字段。
            if p is not None:
                if local_recompose and decision.get("itinerary_draft"):
                    itinerary = list(decision["itinerary_draft"])[:20]
                    locked_titles = [
                        str(item.get("candidate_title") or "").strip()
                        for item in itinerary
                        if str(item.get("candidate_title") or "").strip()
                    ]
                    ledger = {
                        **dict(latest_results.get("plan_ledger") or {}),
                        "locked_candidate_titles": list(
                            dict.fromkeys(locked_titles)
                        ),
                        "current_itinerary": itinerary,
                        "selected_candidate_titles": [
                            str(item.get("title") or "").strip()
                            for item in (
                                latest_results.get("activities") or []
                            )
                            if str(item.get("title") or "").strip()
                        ],
                    }
                    recomposed_payload = {
                        **(
                            latest_bundle_payload
                            or {
                                "activities": list(
                                    latest_results.get("activities") or []
                                ),
                                "research_context": dict(
                                    latest_results.get("research_context") or {}
                                ),
                                "research_selection": dict(
                                    latest_results.get("research_selection") or {}
                                ),
                                "research_artifacts": list(
                                    latest_results.get("research_artifacts") or []
                                ),
                                "transport": (
                                    latest_results.get("transport") or {}
                                ),
                                "warnings": list(
                                    latest_results.get("warnings") or []
                                ),
                            }
                        ),
                        "assistant_response": (
                            decision.get("reply") or ""
                        ),
                        "itinerary_draft": itinerary,
                        "plan_ledger": ledger,
                        "plan_delta": dict(
                            decision.get("plan_delta")
                            or latest_results.get("plan_delta")
                            or {}
                        ),
                    }
                    s.add(TripBundle(
                        plan_id=int(p.id),
                        version="explore",
                        payload=recomposed_payload,
                    ))
                    decision["plan_ledger"] = ledger
                persisted = list(p.conversation or [])
                persisted.extend([
                    {
                        "role": "user",
                        "content": body.message,
                    },
                    {
                        "role": "assistant",
                        "content": decision.get("reply") or "",
                        "intent": decision.get("intent"),
                        "acts": decision.get("acts") or [],
                        "pending_clarify": decision.get("pending_clarify") or [],
                        "itinerary_draft": list(
                            decision.get("itinerary_draft") or []
                        )[:12],
                        "memory_note": decision.get("memory_note"),
                    },
                ])
                p.conversation = persisted[-200:]
                s.commit()
        except Exception:  # 对话永不 500：任何异常→优雅降级（不阻断用户）
            logger.exception("chat turn degraded for plan_id=%s", plan_id)
            try:
                s.rollback()
            except Exception:
                pass
            decision = {
                "plan_id": (str(p.id) if p else None), "intent": "chitchat", "action": "answer",
                "reply": "抱歉，我刚没接住这句话，能再说一次吗？（从哪出发 / 哪个周末 / 想玩什么）",
                "constraints_patch": None, "booking": None, "pending_clarify": [],
                "constraints": memory_ctx, "ready_to_plan": False,
            }
    return JSONResponse(decision)


@app.get("/plans/{plan_id}/calendar.ics")
def calendar_ics(plan_id: str, token: str | None = None):
    """DD-13 ICS 动态订阅（RFC5545，零落盘，恒 200）。token 校验可后续启用。"""
    _require_plan(plan_id)
    bundle = _latest_bundle(plan_id)
    try:
        ics = build_ics(bundle) if bundle else build_ics_fallback({"plan_id": plan_id})
    except Exception:
        ics = build_ics_fallback({"plan_id": plan_id})
    return PlainTextResponse(ics, media_type="text/calendar; charset=utf-8")


def _latest_bundle(plan_id: str) -> dict:
    with get_session() as s:
        row = (
            s.query(TripBundle)
            .filter_by(plan_id=int(plan_id))
            .order_by(TripBundle.created_at.desc())
            .first()
        )
        return row.payload if row else {}


@app.post("/plans/{plan_id}/bookings/import")
def import_booking(plan_id: str, body: ImportBody):
    """DD-14 扩展回填入口（复用 DD-10）：抽取初稿 + 逐字段确认 + 确认态落库。返回确认态。"""
    from ..domain.backfill import confirm_booking, run_extract

    _require_plan(plan_id)
    draft = run_extract(body.kind, body.input_kind, body.raw) if body.raw else {
        "kind": body.kind, "input_kind": body.input_kind, "extracted": body.extracted}
    confirmed = confirm_booking(draft, body.extracted)  # 前端已确认字段覆盖初稿
    if confirmed.get("confirmed"):
        _persist_booking(plan_id, confirmed, body.raw)
    return JSONResponse({"plan_id": plan_id, "ready_for_resume": confirmed["confirmed"],
                         "booking": confirmed})


# ============================ 多人协作（DD-07 §5：匿名邀请 + 聚合）============================
@app.post("/plans/{plan_id}/invites")
def create_invites(plan_id: str, body: InviteBody):
    """生成匿名邀请（plan_members + invite_token）。同伴凭 token 填写，不暴露组织者信息。"""
    _require_plan(plan_id)
    out = []
    with get_session() as s:
        base = s.query(PlanMember).filter_by(plan_id=int(plan_id), is_organizer=False).count()
        for i in range(body.count):
            label = f"同伴{base + i + 1}"
            member = PlanMember(plan_id=int(plan_id), invite_token=secrets.token_urlsafe(12),
                                anon_label=label, is_organizer=False)
            s.add(member)
            s.flush()
            out.append({"token": member.invite_token, "anon_label": label})
        s.commit()
    return JSONResponse({"plan_id": plan_id, "invites": out})


@app.get("/invite/{token}")
def view_invite(token: str):
    """同伴打开邀请：返回 plan_id + 匿名标签（不泄露组织者/他人）。"""
    with get_session() as s:
        m = s.query(PlanMember).filter_by(invite_token=token).one_or_none()
        if not m:
            return JSONResponse({"detail": "邀请无效或已关闭"}, status_code=404)
        return JSONResponse({"plan_id": m.plan_id, "anon_label": m.anon_label})


@app.post("/invite/{token}/constraints")
def submit_member_constraints(token: str, body: MemberConstraintsBody):
    """同伴填写自己的约束（脱敏商圈级）→ party_constraints。"""
    with get_session() as s:
        m = s.query(PlanMember).filter_by(invite_token=token).one_or_none()
        if not m:
            return JSONResponse({"detail": "邀请无效"}, status_code=404)
        pc = s.query(PartyConstraint).filter_by(member_id=m.id).one_or_none()
        if pc is None:
            pc = PartyConstraint(plan_id=m.plan_id, member_id=m.id)
            s.add(pc)
        pc.origin_area = body.origin_area
        pc.earliest_depart = _dt(body.earliest_depart)
        pc.latest_return = _dt(body.latest_return)
        pc.budget_band = body.budget_band
        pc.prefer_flight = (
            body.prefer_flight if body.prefer_flight is not None else body.accept_flight
        )
        pc.accept_night_train = body.accept_night_train
        pc.prefs = body.interests or []
        pc.dietary = body.dietary or []
        if not m.joined_at:
            m.joined_at = datetime.now(timezone.utc)
        s.commit()
        return JSONResponse({"ok": True, "anon_label": m.anon_label})


@app.get("/plans/{plan_id}/party/aggregate")
def party_aggregate(plan_id: str):
    """聚合所有同伴约束（aggregate_party 公平性）→ 合并进 plan.constraints。仅展示聚合（不暴露个人）。"""
    _require_plan(plan_id)
    with get_session() as s:
        members = [
            {
                "earliest_depart": pc.earliest_depart.isoformat() if pc.earliest_depart else None,
                "latest_return": pc.latest_return.isoformat() if pc.latest_return else None,
                "budget_band": pc.budget_band, "accept_flight": pc.prefer_flight,
                "accept_night_train": pc.accept_night_train, "interests": pc.prefs or [],
                "dietary": pc.dietary or [], "origin_area": pc.origin_area,
            }
            for pc in s.query(PartyConstraint).filter_by(plan_id=int(plan_id)).all()
        ]
        if not members:
            return JSONResponse({"plan_id": plan_id, "aggregated": None, "members": 0})
        agg = aggregate_party(members)
        # 合并进 plan.constraints（组织者+聚合）
        p = s.get(Plan, int(plan_id))
        if p:
            c = dict(p.constraints or {})
            if agg.get("earliest_depart"):
                c["earliest_depart"] = agg["earliest_depart"]
            if agg.get("latest_return"):
                c["latest_return"] = agg["latest_return"]
            if agg.get("budget_band"):
                c["budget_band"] = agg["budget_band"]
            c["accept_flight"] = agg.get("accept_flight", True)
            c["accept_night_train"] = agg.get("accept_night_train", False)
            c["interests"] = agg.get("interests", [])
            c["dietary"] = agg.get("dietary", [])
            c["party_size"] = agg.get("party_size", len(members))
            c["origins"] = [m["origin_area"] for m in members if m.get("origin_area")]
            p.constraints = c
            s.commit()
        return JSONResponse({"plan_id": plan_id, "aggregated": agg, "members": len(members)})


def _dt(iso: str | None):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


@app.get("/plans/{plan_id}/state")
def plan_state(plan_id: str):
    _require_plan(plan_id)
    snap = get_planner().get_state(plan_id, thread_id=_load_thread_id(plan_id))
    values = getattr(snap, "values", {}) or {}
    return JSONResponse(json.loads(json.dumps(values, ensure_ascii=False, default=str)))


@app.get("/plans/{plan_id}/bundle")
def plan_bundle(plan_id: str):
    """DD-13：从 trip_bundles 表恢复最新探索版/确认版 bundle。

    图 checkpoint 的 state values 不含 bundle 大对象（interrupt/done 时只落库），
    页面刷新后经 /state 拿不到 → 前端用本端点兜底（web-v2 plan 页，DD-19 联调修复）。
    """
    _require_plan(plan_id)
    with get_session() as s:
        rows = (
            s.query(TripBundle)
            .filter_by(plan_id=int(plan_id))
            .order_by(TripBundle.created_at.desc())
            .all()
        )
    out: dict = {}
    for row in rows:  # 已按时间倒序，setdefault 保留每个 version 的最新一条
        out.setdefault(row.version, row.payload)
    return JSONResponse(json.loads(json.dumps(out, ensure_ascii=False, default=str)))


# ---------------- 静态前端（增补 B：证据六态可视化）----------------
_WEB_DIR = Path(__file__).resolve().parents[3] / "web"
if _WEB_DIR.exists():
    app.mount("/ui", StaticFiles(directory=str(_WEB_DIR), html=True), name="ui")
