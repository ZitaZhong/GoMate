"""流事件 → TripBundle/会话落库（BFF 与 Worker 共用）。

从 bff/app.py 提取（行为保持不变）：interrupt → 探索版 bundle + research_result 会话轮；
done → 确认版 bundle。BFF 原处委托到这里，v4 Supervisor 复用同一段逻辑。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Plan, TripBundle


def compact_agent_activity(item: dict) -> dict:
    """活动卡片的紧凑视图（对话持久化/上下文注入共用）。"""
    evidence = dict(item.get("evidence") or {})
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "candidate_type": item.get("candidate_type"),
        "candidate_kind": item.get("candidate_kind") or item.get("category"),
        "venue": item.get("venue"),
        "description": item.get("description"),
        "start_at": item.get("start_at"),
        "end_at": item.get("end_at"),
        "availability_mode": item.get("availability_mode"),
        "availability": item.get("availability") or {},
        "claims": list(item.get("claims") or [])[:8],
        "subgoal_ids": item.get("subgoal_ids") or [],
        "research_task_ids": item.get("research_task_ids") or [],
        "origin": item.get("origin"),
        "semantic_evaluation": item.get("semantic_evaluation") or {},
        "source_url": evidence.get("source_url"),
        "verification_status": (
            item.get("verification_status")
            or evidence.get("verification_status")
        ),
    }


def agent_results_from_bundle(payload: dict | None) -> dict:
    """探索版 bundle → 研究工作区视图（对话上下文注入用）。"""
    value = dict(payload or {})
    return {
        "activities": [
            compact_agent_activity(item)
            for item in (value.get("activities") or [])[:30]
        ],
        "itinerary_draft": list(value.get("itinerary_draft") or [])[:12],
        "plan_ledger": dict(value.get("plan_ledger") or {}),
        "plan_delta": dict(value.get("plan_delta") or {}),
        "research_context": dict(value.get("research_context") or {}),
        "research_selection": dict(value.get("research_selection") or {}),
        "research_artifacts": list(value.get("research_artifacts") or [])[-8:],
        "assistant_response": value.get("assistant_response"),
        "transport": value.get("transport") or {},
        "warnings": list(value.get("warnings") or [])[:12],
        "version": value.get("version"),
    }


def apply_stream_event(session: Session, plan_id: str, ev: dict) -> TripBundle | None:
    """把探索版/确认版 bundle 落库（DD-13 trip_bundles），并推进 plan.stage。

    返回新建的 TripBundle（若有），供 v4 Run 记录 result_bundle_id。
    """
    p = session.get(Plan, int(plan_id))
    bundle_row: TripBundle | None = None
    if ev["event"] == "interrupt":
        payload = (ev.get("data") or {}).get("explore_bundle")
        if payload:
            bundle_row = TripBundle(plan_id=int(plan_id), version="explore", payload=payload)
            session.add(bundle_row)
        if p:
            p.stage = "await_booking"
            response = str((payload or {}).get("assistant_response") or "").strip()
            if response:
                conversation = list(p.conversation or [])
                result_turn = {
                    "role": "assistant",
                    "content": response,
                    "intent": "research_result",
                    "research_context": dict(
                        (payload or {}).get("research_context") or {}
                    ),
                    "itinerary_draft": list(
                        (payload or {}).get("itinerary_draft") or []
                    )[:12],
                    "plan_ledger": dict(
                        (payload or {}).get("plan_ledger") or {}
                    ),
                    "plan_delta": dict(
                        (payload or {}).get("plan_delta") or {}
                    ),
                    "memory_note": (
                        ((payload or {}).get("research_context") or {}).get(
                            "summary"
                        )
                    ),
                    "cards": [
                        compact_agent_activity(item)
                        for item in ((payload or {}).get("activities") or [])[:30]
                    ],
                }
                if not (
                    conversation
                    and conversation[-1].get("role") == "assistant"
                    and conversation[-1].get("content") == response
                ):
                    conversation.append(result_turn)
                p.conversation = conversation[-200:]
    elif ev["event"] == "done":
        payload = (ev.get("data") or {}).get("bundle")
        if payload:
            bundle_row = TripBundle(plan_id=int(plan_id), version="confirm", payload=payload)
            session.add(bundle_row)
        if p:
            p.stage = "confirm"
    return bundle_row


def persist_stream_event(plan_id: str, ev: dict, session: Session | None = None) -> None:
    """独立事务版本（BFF 旧 SSE 路径使用；行为与原 _persist_event 一致）。"""
    if session is not None:
        apply_stream_event(session, plan_id, ev)
        return
    with get_session() as s:
        apply_stream_event(s, plan_id, ev)
