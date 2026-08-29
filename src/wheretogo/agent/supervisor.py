"""Run Supervisor：执行持久化的 AgentRun（v4 §9.4）。

浏览器不再"启动"工作：Worker 领取 Outbox 后在服务端执行完整生命周期——
加载上下文 → 执行 → 发布进度事件 → 维护心跳 → 聚合部分成功 → 原子提交终态。
"""
from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..db import get_session
from ..infra.redis_client import plan_lock
from ..models import AgentRun, AgentTurn, Plan, TripBundle
from . import events
from .persist import apply_stream_event
from .status import ErrorCode, RunStatus, TurnStatus, user_facing_error

logger = logging.getLogger(__name__)

_planner = None


def get_planner():
    """Worker 侧惰性单例：优先 Postgres checkpointer，失败回退内存。"""
    global _planner
    if _planner is None:
        from ..orchestration import PlannerService

        try:
            from ..orchestration import make_postgres_checkpointer

            _planner = PlannerService(make_postgres_checkpointer())
        except Exception:
            _planner = PlannerService()
    return _planner


@contextmanager
def _db(session: Session | None):
    if session is not None:
        yield session
        session.flush()
    else:
        with get_session() as s:
            yield s


def _maybe_commit(s: Session, own: bool) -> None:
    """自持 session 时增量提交：进度事件/心跳必须对 SSE 和 stall 检测实时可见。

    测试传入 SAVEPOINT session 时不提交，由外层事务统一回滚。
    """
    if own:
        s.commit()


def _finalize(
    session: Session,
    run_id: uuid.UUID,
    *,
    run_status: RunStatus,
    turn_status: TurnStatus,
    visible_reply: str | None,
    error_code: str | None = None,
    result_bundle_id: int | None = None,
) -> None:
    """原子提交 Run 终态 + Turn 终态 + 最终助手消息（同一事务）。"""
    run = session.get(AgentRun, run_id)
    now = datetime.now(timezone.utc)
    run.status = run_status.value
    run.completed_at = now
    run.error_code = error_code
    if result_bundle_id is not None:
        run.result_bundle_id = result_bundle_id
    turn = session.get(AgentTurn, run.turn_id) if run.turn_id else None
    if turn is not None:
        turn.status = turn_status.value
        if visible_reply:
            turn.visible_reply = visible_reply
        turn.error_code = error_code
        turn.completed_at = now
        turn.updated_at = now
    events.publish(
        session, run_id, "run.status",
        phase=run_status.value,
        message=visible_reply or "",
        payload={
            "final": True,
            "turn_status": turn_status.value,
            "error": user_facing_error(error_code) if error_code else None,
        },
    )


def _is_cancel_requested(session: Session, run_id: uuid.UUID) -> bool:
    session.expire_all()
    run = session.get(AgentRun, run_id)
    return bool(run and run.cancel_requested)


def _recompose(session: Session, run: AgentRun, plan: Plan) -> tuple[dict | None, str]:
    """本地重排：只使用当前 Workspace，不做外部搜索（v4 场景 D）。"""
    plan_id = str(plan.id)
    bundle_row = (
        session.query(TripBundle)
        .filter_by(plan_id=plan.id, version="explore")
        .order_by(TripBundle.created_at.desc())
        .first()
    )
    base_payload = dict(bundle_row.payload or {}) if bundle_row else {}
    itinerary = list((run.execution_plan or {}).get("itinerary_draft") or [])
    if not itinerary:
        itinerary = list(base_payload.get("itinerary_draft") or [])[:20]
    if not base_payload and not itinerary:
        return None, "当前没有可重排的候选结果。"
    locked_titles = [
        str(item.get("candidate_title") or "").strip()
        for item in itinerary
        if str(item.get("candidate_title") or "").strip()
    ]
    ledger = {
        **dict(base_payload.get("plan_ledger") or {}),
        "locked_candidate_titles": list(dict.fromkeys(locked_titles)),
        "current_itinerary": itinerary,
        "selected_candidate_titles": [
            str(item.get("title") or "").strip()
            for item in (base_payload.get("activities") or [])
            if str(item.get("title") or "").strip()
        ],
    }
    lines = [
        f"{item.get('day') or '周末'} {item.get('time_window') or ''} "
        f"{item.get('candidate_title') or ''}".strip()
        + (f"——{str(item.get('reason') or '').strip()}" if str(item.get('reason') or '').strip() else "")
        for item in itinerary
    ]
    reply = (
        "已按你的要求基于现有候选重排行程（未做外部搜索），完整安排如下：\n"
        + "\n".join(f"· {line}" for line in lines)
        if lines else "已按你的要求确认当前安排（未做外部搜索）。"
    )
    payload = {
        **base_payload,
        "assistant_response": reply,
        "itinerary_draft": itinerary,
        "plan_ledger": ledger,
        "plan_delta": {
            "recomposed": True,
            "recomputed": locked_titles,
            "preserved": ledger["selected_candidate_titles"],
            "instruction": (run.execution_plan or {}).get("instruction"),
        },
    }
    bundle = apply_stream_event(
        session, plan_id, {"event": "interrupt", "data": {"explore_bundle": payload}}
    )
    session.flush()
    return (payload if bundle is None else payload), reply


def _stream_events(planner, run: AgentRun, plan: Plan, mode: str):
    """按执行模式产出 planner 事件流。"""
    plan_id = str(plan.id)
    thread_id = run.checkpoint_ref or plan.thread_id
    exec_plan = dict(run.execution_plan or {})
    if mode == "research_more":
        feedback = str(exec_plan.get("feedback") or exec_plan.get("instruction") or "")
        prepared = False
        try:
            snap = planner.get_state(plan_id, thread_id=thread_id)
            values = getattr(snap, "values", {}) or {}
            if values.get("constraints"):
                planner.prepare_research_more(
                    plan_id,
                    feedback,
                    revision_mode=str(exec_plan.get("revision_mode") or "alternative"),
                    constraints=dict(plan.constraints or {}),
                    thread_id=thread_id,
                    conversation=list(plan.conversation or []),
                    plan_ledger=dict(values.get("plan_ledger") or {}),
                    itinerary_draft=list(values.get("itinerary_draft") or []),
                )
                prepared = True
        except Exception:
            logger.exception("prepare_research_more failed run=%s", run.id)
        if prepared:
            return planner.stream_research_more(plan_id, thread_id=thread_id)
        # checkpoint 缺失 → 降级为全量启动（不静默失败）
        mode = "start"
    if mode == "replan_weather":
        return planner.stream_replan(
            plan_id,
            str(exec_plan.get("reason") or exec_plan.get("instruction") or ""),
            "weather",
            thread_id=thread_id,
        )
    # start：完整规划图
    try:
        return planner.stream_start(
            plan_id,
            dict(plan.constraints or {}),
            conversation=list(plan.conversation or []),
            thread_id=thread_id,
        )
    except TypeError:  # 轻量测试 planner 适配
        return planner.stream_start(plan_id, dict(plan.constraints or {}), thread_id=thread_id)


def _event_phase(ev: dict) -> str | None:
    data = ev.get("data")
    if isinstance(data, dict):
        for key in ("phase", "stage", "step"):
            if data.get(key):
                return str(data[key])
    return str(ev.get("node") or "") or None


def _compact_event_payload(ev: dict) -> dict:
    """RunEvent 只保留 UI 需要的进度信息，不写入完整节点输出。"""
    data = ev.get("data")
    if not isinstance(data, dict):
        return {"raw": str(data)[:500]} if data is not None else {}
    keep: dict = {}
    for key in ("message", "detail", "progress", "completed", "total", "round",
                "query", "phase", "count", "degraded", "code"):
        if key in data:
            keep[key] = data[key]
    if not keep:
        keep = {"keys": list(data.keys())[:12]}
    return keep


def execute_run(
    run_id: str | uuid.UUID,
    *,
    session: Session | None = None,
    planner=None,
) -> str:
    """执行一个 AgentRun 至终态；返回最终 RunStatus 值。

    可被 Worker 调用，也可在测试中同步驱动（传入 session + 轻量 planner）。
    """
    rid = uuid.UUID(str(run_id))
    own_session = session is None
    with _db(session) as s:
        run = s.get(AgentRun, rid)
        if run is None:
            return "missing"
        if run.status != RunStatus.QUEUED.value:
            return run.status  # 幂等：已执行/执行中不重复
        if run.cancel_requested:
            _finalize(
                s, rid,
                run_status=RunStatus.CANCELLED,
                turn_status=TurnStatus.CANCELLED,
                visible_reply="任务在开始前被取消。",
            )
            return RunStatus.CANCELLED.value
        plan = s.get(Plan, run.plan_id)
        if plan is None:
            _finalize(
                s, rid,
                run_status=RunStatus.FAILED,
                turn_status=TurnStatus.FAILED,
                visible_reply="关联的规划不存在，任务无法执行。",
                error_code=ErrorCode.PERSISTENCE_FAILED.value,
            )
            return RunStatus.FAILED.value
        now = datetime.now(timezone.utc)
        run.status = RunStatus.RUNNING.value
        run.started_at = run.started_at or now
        run.heartbeat_at = now
        events.publish(s, rid, "run.status", phase="running", message="任务开始执行")
        _maybe_commit(s, own_session)
        mode = str((run.execution_plan or {}).get("mode") or "start")
        plan_id = str(plan.id)

        # —— recompose：本地重排，不进图 ——
        if mode == "recompose":
            try:
                _payload, reply = _recompose(s, run, plan)
                bundle_row = (
                    s.query(TripBundle)
                    .filter_by(plan_id=plan.id, version="explore")
                    .order_by(TripBundle.created_at.desc())
                    .first()
                )
                _finalize(
                    s, rid,
                    run_status=RunStatus.SUCCEEDED,
                    turn_status=TurnStatus.ANSWERED,
                    visible_reply=reply,
                    result_bundle_id=(bundle_row.id if bundle_row else None),
                )
                return RunStatus.SUCCEEDED.value
            except Exception:
                logger.exception("recompose run failed run=%s", rid)
                _finalize(
                    s, rid,
                    run_status=RunStatus.FAILED,
                    turn_status=TurnStatus.FAILED,
                    visible_reply=user_facing_error(
                        ErrorCode.COMPOSITION_FAILED.value
                    )["message"],
                    error_code=ErrorCode.COMPOSITION_FAILED.value,
                )
                return RunStatus.FAILED.value

        # —— 图执行（start / research_more / replan_weather）——
        planner = planner or get_planner()
        final_payload: dict | None = None
        final_bundle_id: int | None = None
        saw_error = False
        degraded_message = ""
        cancelled = False
        try:
            with plan_lock(plan_id, timeout=900, blocking_timeout=5):
                stream = _stream_events(planner, run, plan, mode)
                for ev in stream:
                    if _is_cancel_requested(s, rid):
                        cancelled = True
                        break
                    kind = str(ev.get("event") or "")
                    if kind == "progress":
                        events.publish(
                            s, rid, "research.progress",
                            phase=_event_phase(ev),
                            message=str(
                                (ev.get("data") or {}).get("message")
                                if isinstance(ev.get("data"), dict) else ""
                            ) or None,
                            payload=_compact_event_payload(ev),
                        )
                        _maybe_commit(s, own_session)
                    elif kind in {"interrupt", "done"}:
                        bundle_row = apply_stream_event(s, plan_id, ev)
                        s.flush()
                        data = ev.get("data") or {}
                        final_payload = (
                            data.get("explore_bundle")
                            if kind == "interrupt" else data.get("bundle")
                        ) or {}
                        if bundle_row is not None:
                            final_bundle_id = bundle_row.id
                        events.publish(
                            s, rid, "run.result",
                            phase="composing",
                            message="已生成方案",
                            payload={"kind": kind},
                        )
                        _maybe_commit(s, own_session)
                    elif kind == "error":
                        saw_error = True
                        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
                        degraded_message = str(data.get("message") or "")
                        events.publish(
                            s, rid, "run.error",
                            phase=_event_phase(ev),
                            message=degraded_message or "执行出错",
                            payload=_compact_event_payload(ev),
                        )
                        _maybe_commit(s, own_session)
                    else:  # node_output 等
                        events.publish(
                            s, rid, "run.node",
                            phase=str(ev.get("node") or "") or None,
                            payload={"node": ev.get("node")},
                        )
                        _maybe_commit(s, own_session)
        except TimeoutError:
            _finalize(
                s, rid,
                run_status=RunStatus.FAILED,
                turn_status=TurnStatus.FAILED,
                visible_reply="该方案有其他任务正在执行，请稍后重试。",
                error_code=ErrorCode.RUN_STALLED.value,
            )
            return RunStatus.FAILED.value
        except Exception:
            logger.exception("run execution crashed run=%s", rid)
            saw_error = True

        if cancelled:
            _finalize(
                s, rid,
                run_status=RunStatus.CANCELLED,
                turn_status=TurnStatus.CANCELLED,
                visible_reply="任务已按你的要求取消；已获得的结果保留。",
            )
            return RunStatus.CANCELLED.value

        # —— 聚合终态：部分成功不丢弃、失败必须诚实（v4 §13）——
        if final_payload is not None:
            reply = str(final_payload.get("assistant_response") or "").strip()
            activities = list(final_payload.get("activities") or [])
            activity_count = len(activities)
            if not reply:
                # 回复合成失败：结构化降级，但仍自洽（内联已得候选名），Turn=PARTIAL（§13.4）。
                names = [
                    str(a.get("title") or "").strip()
                    for a in activities[:5]
                    if str(a.get("title") or "").strip()
                ]
                listed = ("：" + "、".join(names)) if names else ""
                reply = (
                    f"本轮已获得 {activity_count} 个有来源的候选{listed}。"
                    "完整文字总结未能生成，你可以让我重试生成总结。"
                )
                _finalize(
                    s, rid,
                    run_status=RunStatus.PARTIAL,
                    turn_status=TurnStatus.PARTIAL,
                    visible_reply=reply,
                    error_code=ErrorCode.COMPOSITION_FAILED.value,
                    result_bundle_id=final_bundle_id,
                )
                return RunStatus.PARTIAL.value
            if saw_error:
                _finalize(
                    s, rid,
                    run_status=RunStatus.PARTIAL,
                    turn_status=TurnStatus.PARTIAL,
                    visible_reply=reply + "\n（部分来源执行失败，结果可能不完整，可以让我继续补充。）",
                    error_code=ErrorCode.PARTIAL_EVIDENCE.value,
                    result_bundle_id=final_bundle_id,
                )
                return RunStatus.PARTIAL.value
            _finalize(
                s, rid,
                run_status=RunStatus.SUCCEEDED,
                turn_status=TurnStatus.ANSWERED,
                visible_reply=reply,
                result_bundle_id=final_bundle_id,
            )
            return RunStatus.SUCCEEDED.value

        # 无最终产物：错误→失败；无错误也不能静默（超时/中断→失败并说明）。
        err = user_facing_error(
            ErrorCode.TOOL_TIMEOUT.value if not saw_error else ErrorCode.PARTIAL_EVIDENCE.value
        )
        reply = degraded_message or f"{err['message']}{err['recovery']}"
        _finalize(
            s, rid,
            run_status=RunStatus.FAILED,
            turn_status=TurnStatus.FAILED,
            visible_reply=reply,
            error_code=err["code"],
        )
        return RunStatus.FAILED.value
