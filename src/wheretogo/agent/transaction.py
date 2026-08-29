"""Turn Transaction：原子提交 Turn / Clarification / Run / Outbox（v4 §9.2）。

流水线：interpret_turn → resolve_prerequisites → build_runtime_decision →
persist(原子) → validate_turn_contract。回复承诺执行研究时，真实任务必须已创建；
任务创建失败必须诚实回复失败，不允许输出未来时承诺。
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..copilot.handle_turn import (
    _answer_from_db,
    _apply_city_code,
    _looks_like_booking,
    _looks_like_explicit_refinement,
    _looks_like_route_design,
    _rule_intent,
    build_route_plan,
)
from ..copilot.interpreter import interpret_turn
from ..db import get_session
from ..models import (
    AgentOutbox,
    AgentRun,
    AgentTurn,
    ClarificationRequest,
    Plan,
    TripBundle,
)
from . import events
from .decision import (
    build_runtime_decision,
    validate_turn_contract,
)
from .persist import agent_results_from_bundle
from .prerequisites import derive_known_facts, resolve_prerequisites
from .status import (
    RUN_ACTIVE,
    ErrorCode,
    RunStatus,
    RunType,
    TurnStatus,
    user_facing_error,
)

logger = logging.getLogger(__name__)

#: 核心检索身份字段：变化必须新线程重跑 discovery（沿用旧 chat handler 判定集合）
_IDENTITY_KEYS = {
    "origins",
    "target_city_code",
    "target_city_name",
    "weekend_start",
    "weekend_end",
    "earliest_depart",
    "latest_return",
}

#: 澄清事实名 → 旧协议槽位名（复用 interpreter 的短答案绑定）
_FACT_TO_SLOT = {"origin": "origins", "origins": "origins"}


@contextmanager
def _session_scope(session: Session | None):
    """外部传入 session（测试 SAVEPOINT）→ 只 flush；否则独立事务。"""
    if session is not None:
        yield session
        session.flush()
    else:
        with get_session() as s:
            yield s


def _next_sequence_no(session: Session, plan_id: int) -> int:
    return (
        session.query(func.coalesce(func.max(AgentTurn.sequence_no), 0))
        .filter(AgentTurn.plan_id == plan_id)
        .scalar()
    ) + 1


def _latest_results(session: Session, plan: Plan) -> dict:
    """探索版 bundle + 会话 research_result 轮 → 研究工作区视图。"""
    results: dict = {}
    bundle_row = (
        session.query(TripBundle)
        .filter_by(plan_id=int(plan.id), version="explore")
        .order_by(TripBundle.created_at.desc())
        .first()
    )
    if bundle_row:
        results = agent_results_from_bundle(bundle_row.payload)
    for turn in reversed(list(plan.conversation or [])):
        if turn.get("intent") == "research_result":
            persisted = {
                "activities": list(turn.get("cards") or [])[:30],
                "itinerary_draft": list(turn.get("itinerary_draft") or [])[:12],
                "research_context": dict(turn.get("research_context") or {}),
                "plan_ledger": dict(turn.get("plan_ledger") or {}),
                "plan_delta": dict(turn.get("plan_delta") or {}),
                "assistant_response": turn.get("content"),
            }
            results = {
                **persisted,
                **{k: v for k, v in results.items() if v not in (None, [], {})},
            }
            break
    return results


def _active_run(session: Session, plan_id: int) -> AgentRun | None:
    return (
        session.query(AgentRun)
        .filter(
            AgentRun.plan_id == plan_id,
            AgentRun.status.in_([s.value for s in RUN_ACTIVE]),
        )
        .order_by(AgentRun.created_at.desc())
        .first()
    )


def _open_clarifications(session: Session, plan_id: int) -> list[ClarificationRequest]:
    return (
        session.query(ClarificationRequest)
        .join(AgentTurn, ClarificationRequest.turn_id == AgentTurn.id)
        .filter(AgentTurn.plan_id == plan_id, ClarificationRequest.status == "open")
        .order_by(ClarificationRequest.created_at.desc())
        .all()
    )


def _pending_clarify_ctx(clarifications: list[ClarificationRequest]) -> list[dict]:
    """open 澄清 → 旧协议 [{slot, q}]，供 interpreter 绑定短答案。"""
    pending: list[dict] = []
    for clar in clarifications:
        facts = list(clar.requested_facts or [])
        fact = str((facts[0] or {}).get("name") if facts else "") or ""
        slot = _FACT_TO_SLOT.get(fact, fact or "info")
        pending.append({"slot": slot, "q": clar.question})
    return pending


def _merge_constraints_patch(plan: Plan, patch: dict) -> tuple[dict, bool, str]:
    """把补丁并入 plan.constraints；返回 (有效补丁, 是否身份变化, revision_mode)。"""
    current = dict(plan.constraints or {})
    patch = dict(patch or {})
    patch.pop("__research_feedback", None)
    revision_mode = "alternative"
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
    if old_requirements and old_requirements < new_requirements:
        revision_mode = "extend"
    elif "experience_requirements" in patch or "research_subgoals" in patch:
        revision_mode = "replace"
    if patch.get("soft_preferences"):
        patch["soft_preferences"] = list(dict.fromkeys(
            (current.get("soft_preferences") or []) + list(patch["soft_preferences"])
        ))
    patch = {key: value for key, value in patch.items() if current.get(key) != value}
    requires_full_replan = bool(set(patch) & _IDENTITY_KEYS)
    if patch:
        merged = {**current, **patch}
        if any(key in patch for key in (
            "interests", "soft_preferences", "experience_requirements",
            "research_goal", "acceptance_criteria", "research_subgoals",
            "budget_band", "dietary",
            "target_city_code", "weekend_start", "weekend_end",
        )):
            merged.pop("query", None)  # query 是派生字段，检索语义变化必须失效
        plan.constraints = merged
    return patch, requires_full_replan, revision_mode


def _idempotent_response(session: Session, turn: AgentTurn) -> dict:
    """重复 Idempotency-Key → 返回同一个 Turn/Run（v4 §9.3）。"""
    run = session.get(AgentRun, turn.run_id) if turn.run_id else None
    clar = (
        session.get(ClarificationRequest, turn.clarification_id)
        if turn.clarification_id else None
    )
    return _response(turn, run, clar, idempotent=True)


def _clarification_dict(clar: ClarificationRequest | None) -> dict | None:
    if clar is None:
        return None
    return {
        "id": str(clar.id),
        "blocking": bool(clar.blocking),
        "question": clar.question,
        "reason": clar.reason or "",
        "requested_facts": list(clar.requested_facts or []),
        "assumptions_if_skipped": list(clar.assumptions_if_skipped or []),
        "status": clar.status,
    }


def _run_dict(run: AgentRun | None) -> dict | None:
    if run is None:
        return None
    return {
        "id": str(run.id),
        "status": run.status,
        "type": run.run_type,
        "goal": run.goal,
        "parent_run_id": str(run.parent_run_id) if run.parent_run_id else None,
        "assumptions": list(run.assumptions or []),
        "events_url": f"/agent/runs/{run.id}/events",
    }


def _response(
    turn: AgentTurn,
    run: AgentRun | None,
    clar: ClarificationRequest | None,
    *,
    booking: dict | None = None,
    idempotent: bool = False,
) -> dict:
    mode = str((run.execution_plan or {}).get("mode") or "") if run else ""
    payload = {
        "plan_id": str(turn.plan_id),
        "conversation_id": str(turn.plan_id),
        "turn_id": str(turn.id),
        "turn_status": turn.status,
        "assistant_message": {"content": turn.visible_reply or ""},
        "run": _run_dict(run),
        "clarification": _clarification_dict(clar),
        "error": (
            user_facing_error(turn.error_code) if turn.error_code else None
        ),
        "idempotent": idempotent,
        # —— 旧字段适配器（由新状态派生，仅供过渡观察；新前端不使用）——
        "reply": turn.visible_reply or "",
        "auto_stream": mode == "research_more",
        "restart_stream": mode in {"start", "replan_weather"},
        "ready_to_plan": run is not None,
    }
    if booking:
        payload["booking"] = booking
    return payload


def commit_turn(
    plan_id: str | None,
    message: str,
    *,
    client_key: str | None = None,
    session: Session | None = None,
    use_llm: bool = True,
) -> dict:
    """处理一轮用户消息：解释 → 前置解析 → 运行时决定 → 原子持久化。

    返回 Chat API 响应字典。plan 不存在时抛 LookupError（API 映射 404）。
    """
    message = (message or "").strip()
    with _session_scope(session) as s:
        # —— plan 解析/创建（chat-first：new → 自动建 plan，Turn 需要 FK）——
        if plan_id and str(plan_id).isdigit():
            plan = s.get(Plan, int(plan_id))
            if plan is None:
                raise LookupError(f"plan {plan_id} 不存在")
        else:
            plan = Plan(stage="explore", thread_id=f"pending-{uuid.uuid4()}", constraints={})
            s.add(plan)
            s.flush()
            plan.thread_id = f"plan:{plan.id}"

        # —— 幂等：同 (plan, client_key) 直接返回既有 Turn/Run（v4 §9.3）——
        if client_key:
            existing = (
                s.query(AgentTurn)
                .filter_by(plan_id=plan.id, client_key=client_key)
                .first()
            )
            if existing is not None:
                return _idempotent_response(s, existing)

        constraints_before = dict(plan.constraints or {})
        conversation = list(plan.conversation or [])
        open_clars = _open_clarifications(s, plan.id)
        pending_ctx = _pending_clarify_ctx(open_clars)
        latest_results = _latest_results(s, plan)
        has_research_result = bool(
            latest_results.get("activities") or latest_results.get("research_context")
        )
        active = _active_run(s, plan.id)
        active_summary = (
            {
                "run_id": str(active.id),
                "type": active.run_type,
                "status": active.status,
                "goal": active.goal,
            }
            if active is not None else None
        )

        turn = AgentTurn(
            plan_id=plan.id,
            sequence_no=_next_sequence_no(s, plan.id),
            user_message=message,
            status=TurnStatus.INTERPRETING.value,
            client_key=client_key,
        )
        s.add(turn)
        s.flush()

        # —— 解释（模型理解开放世界；失败→确定性规则兜底，绝不静默）——
        try:
            fallback_intent = (
                "confirm_booking" if _looks_like_booking(message)
                else "refine_field" if _looks_like_explicit_refinement(message)
                else _rule_intent((message or "").lower()) or "provide_constraints"
            )
            interpreted = interpret_turn(
                message,
                fallback_intent=fallback_intent,
                memory_ctx=constraints_before,
                conversation=conversation,
                stage=plan.stage,
                pending_clarify=pending_ctx,
                latest_results=latest_results,
                active_run=active_summary,
                use_llm=use_llm,
            )
        except Exception:
            logger.exception("turn interpretation failed plan_id=%s", plan.id)
            turn.status = TurnStatus.FAILED.value
            turn.error_code = ErrorCode.INTERPRETATION_FAILED.value
            err = user_facing_error(turn.error_code)
            turn.visible_reply = f"{err['message']}{err['recovery']}"
            turn.completed_at = datetime.now(timezone.utc)
            s.flush()
            return _response(turn, None, None)

        extracted = dict(interpreted.constraints_patch or {})
        _apply_city_code(extracted, s)
        turn.interpretation = {
            "primary_intent": interpreted.primary_intent,
            "acts": list(interpreted.acts),
            "goals": list(interpreted.goals),
            "proposed_actions": list(interpreted.proposed_actions),
            "clarification_candidates": list(interpreted.clarification_candidates),
            "constraints_patch": {
                k: v for k, v in extracted.items() if k != "__research_feedback"
            },
            "research_goal": interpreted.research_goal,
            "confidence": interpreted.confidence,
            "interpretation_source": interpreted.interpretation_source,
        }

        patch, requires_full_replan, revision_mode = _merge_constraints_patch(
            plan, extracted
        )
        constraints_now = dict(plan.constraints or {})

        # —— 回答了 open 澄清的事实 → 标记 answered，并收敛旧 NEEDS_INPUT 回合 ——
        facts_now = derive_known_facts(constraints_now, latest_results)
        for clar in open_clars:
            fact_names = {
                str((item or {}).get("name") or "")
                for item in (clar.requested_facts or [])
            }
            if any(facts_now.get(name) for name in fact_names if name):
                clar.status = "answered"
                clar.answer_turn_id = turn.id
                clar.updated_at = datetime.now(timezone.utc)
                prior = s.get(AgentTurn, clar.turn_id)
                if prior is not None and prior.status == TurnStatus.NEEDS_INPUT.value:
                    prior.status = TurnStatus.ANSWERED.value
                    prior.completed_at = datetime.now(timezone.utc)

        # —— 前置条件解析 + 运行时决定（missing_slots 不再拥有执行权）——
        resolution = resolve_prerequisites(
            interpreted.goals, interpreted.proposed_actions, facts_now
        )
        result = build_runtime_decision(
            interpreted,
            resolution,
            has_research_result=has_research_result,
            active_run=active_summary,
        )

        # —— design_itinerary（DD-15 v1.1）：v4 语义下点名排路线优先走 research run
        # （核实场次/位置后给完整行程）；仅当本轮不产生 Run 且无阻塞澄清时，
        # 用库内锚点路线卡同步作答（离线/降级不留白，证据纪律同 legacy /chat）。
        route_plan: dict | None = None
        if (
            _looks_like_route_design(message)
            and result.run is None
            and not (result.clarification is not None and result.clarification.blocking)
        ):
            try:
                route_reply, route_plan, _ = build_route_plan(
                    message, constraints_now, s,
                    city_code=constraints_now.get("target_city_code"),
                    use_llm=use_llm, conversation=conversation, extracted=extracted,
                )
                if route_plan is not None:
                    result.status = TurnStatus.ANSWERED
                    result.visible_reply = route_reply
            except Exception:
                logger.exception("route design fallback failed plan_id=%s", plan.id)
                route_plan = None

        # ask_info 兜底：模型缺席时用库内问答，不退化成固定欢迎语。
        if (
            result.status == TurnStatus.ANSWERED
            and not (interpreted.assistant_reply or "").strip()
            and "answer_info" in (interpreted.acts or [])
        ):
            result.visible_reply = _answer_from_db(
                message, s, constraints_now.get("target_city_code"), use_llm=use_llm
            )

        # —— booking 草稿（抽取只是初稿，需前端确认；不产生 Run）——
        booking: dict | None = None
        if "submit_booking" in (interpreted.acts or []):
            try:
                from ..domain.backfill import run_extract

                draft = run_extract("manual", "text", message)
                if draft.get("extracted"):
                    booking = {**draft, "confirmed": False, "ready_for_resume": False}
            except Exception:
                booking = None

        # —— 持久化澄清 ——
        clar_row: ClarificationRequest | None = None
        if result.clarification is not None:
            clar_row = ClarificationRequest(
                turn_id=turn.id,
                question=result.clarification.question,
                reason=result.clarification.reason,
                blocking=result.clarification.blocking,
                requested_facts=list(result.clarification.requested_facts),
                assumptions_if_skipped=list(result.clarification.assumptions_if_skipped),
                status="open",
            )
            s.add(clar_row)
            s.flush()
            turn.clarification_id = clar_row.id

        # —— 持久化 Run + Outbox（先创建任务，再承诺执行；v4 §4.3）——
        run_row: AgentRun | None = None
        if result.run is not None:
            try:
                draft = result.run
                execution_plan: dict = {"instruction": message}
                if draft.run_type == RunType.RECOMPOSE.value:
                    execution_plan["mode"] = "recompose"
                    execution_plan["itinerary_draft"] = list(
                        interpreted.itinerary_draft or []
                    )
                elif draft.run_type == RunType.REPLAN.value:
                    execution_plan["mode"] = "replan_weather"
                    execution_plan["reason"] = message
                else:  # research
                    if (
                        has_research_result
                        and not requires_full_replan
                        and "research_more" in (interpreted.acts or [])
                    ):
                        execution_plan["mode"] = "research_more"
                        execution_plan["feedback"] = message
                        execution_plan["revision_mode"] = revision_mode
                    else:
                        execution_plan["mode"] = "start"
                        # 已有结果/约束变化 → 新线程重跑 discovery（不串旧 checkpoint）
                        if has_research_result or (patch and requires_full_replan):
                            plan.stage = "explore"
                            plan.thread_id = f"plan:{plan.id}:r{time.time_ns()}"

                parent_run_id = None
                if active is not None:
                    if requires_full_replan:
                        # 核心检索身份变化：取消当前任务，替换执行。
                        active.cancel_requested = True
                        execution_plan["replaces_run_id"] = str(active.id)
                    else:
                        # 追加/重排：child run，父任务结束后由 Worker 领取。
                        parent_run_id = active.id

                run_row = AgentRun(
                    plan_id=plan.id,
                    turn_id=turn.id,
                    parent_run_id=parent_run_id,
                    run_type=draft.run_type,
                    status=RunStatus.QUEUED.value,
                    goal=draft.goal or message,
                    execution_plan=execution_plan,
                    required_inputs={
                        k: v for k, v in facts_now.items() if k != "existing_candidates"
                    },
                    assumptions=list(draft.assumptions),
                    checkpoint_ref=plan.thread_id,
                )
                s.add(run_row)
                s.flush()
                s.add(AgentOutbox(
                    topic="agent_run.requested",
                    payload={"run_id": str(run_row.id), "plan_id": str(plan.id)},
                    status="pending",
                ))
                events.publish(
                    s, run_row.id, "run.status",
                    phase="queued", message="任务已创建，等待执行",
                )
                result.run_persisted = True
                result.run_id = str(run_row.id)
                turn.run_id = run_row.id
            except Exception:
                # 任务创建失败：诚实回复失败，不输出未来时承诺（v4 §4.3）。
                logger.exception("run creation failed plan_id=%s", plan.id)
                run_row = None
                result.status = TurnStatus.FAILED
                result.run = None
                result.run_persisted = False
                result.run_id = None
                result.error_code = ErrorCode.RUN_CREATION_FAILED.value
                err = user_facing_error(result.error_code)
                result.visible_reply = f"{err['message']}{err['recovery']}"
                result.error = err

        # —— Turn 终态 + 合约校验（提交前强制不变量；v4 §8.3）——
        turn.status = result.status.value
        turn.visible_reply = result.visible_reply
        turn.error_code = result.error_code
        if result.status in {TurnStatus.ANSWERED, TurnStatus.FAILED}:
            turn.completed_at = datetime.now(timezone.utc)
        turn.updated_at = datetime.now(timezone.utc)
        validate_turn_contract(result)

        # —— 会话持久化（旧 UI/agent-state 兼容：含 pending_clarify 双呈现）——
        pending_clarify_legacy = (
            [{
                "slot": _FACT_TO_SLOT.get(
                    str((result.clarification.requested_facts or [{}])[0].get("name") or ""),
                    "info",
                ),
                "q": result.clarification.question,
            }]
            if result.clarification and result.clarification.blocking
            else []
        )
        persisted = list(plan.conversation or [])
        persisted.extend([
            {"role": "user", "content": message},
            {
                "role": "assistant",
                "content": result.visible_reply or "",
                "intent": (
                    "design_itinerary" if route_plan is not None
                    else interpreted.primary_intent
                ),
                "acts": list(interpreted.acts or []),
                "pending_clarify": pending_clarify_legacy,
                "itinerary_draft": list(interpreted.itinerary_draft or [])[:12],
                "memory_note": interpreted.memory_note,
                "turn_id": str(turn.id),
                "turn_status": turn.status,
                **({"route_plan": route_plan} if route_plan is not None else {}),
            },
        ])
        plan.conversation = persisted[-200:]
        s.flush()
        payload = _response(turn, run_row, clar_row, booking=booking)
        if route_plan is not None:
            payload["route_plan"] = route_plan
        return payload
