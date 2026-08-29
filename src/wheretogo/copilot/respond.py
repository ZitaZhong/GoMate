"""Compose a conversational answer from plan state and research evidence."""
from __future__ import annotations

import json

from ..config import get_settings
from ..providers import extract_json


def _compact_activity(item: dict) -> dict:
    evidence = dict(item.get("evidence") or {})
    return {
        "title": item.get("title"),
        "kind": item.get("candidate_kind") or item.get("category"),
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
        "verification_status": item.get("verification_status"),
    }


def _conversation_memory(conversation: list[dict]) -> dict:
    """Keep recent dialogue verbatim and older turns as a bounded ledger."""
    usable = [
        {
            "role": item.get("role"),
            "content": str(item.get("content") or "")[:1200],
        }
        for item in conversation
        if item.get("role") in {"user", "assistant"}
        and str(item.get("content") or "").strip()
    ]
    recent = usable[-24:]
    earlier = [
        {
            "turn": index + 1,
            "role": item["role"],
            "content": item["content"][:500],
        }
        for index, item in enumerate(usable[:-24][-80:])
    ]
    return {
        "total_turns": len(usable),
        "recent_turns": recent,
        "earlier_turn_ledger": earlier,
    }


def _plan_ledger(state: dict, activities: list[dict]) -> dict:
    """Build durable commitments separately from lossy conversation summaries."""
    previous = dict(state.get("plan_ledger") or {})
    itinerary = list(state.get("itinerary_draft") or [])
    locked_titles = [
        str(value).strip()
        for value in (previous.get("locked_candidate_titles") or [])
        if str(value).strip()
    ]
    locked_titles.extend(
        str(item.get("candidate_title") or "").strip()
        for item in itinerary
        if str(item.get("candidate_title") or "").strip()
    )
    return {
        **previous,
        "locked_candidate_titles": list(dict.fromkeys(locked_titles)),
        "current_itinerary": itinerary[:20],
        "selected_candidate_titles": [
            str(item.get("title") or "")
            for item in activities
            if str(item.get("title") or "").strip()
        ],
    }


def _validated_plan_delta(
    parsed: dict,
    *,
    activities: list[dict],
    ledger: dict,
) -> dict:
    known_titles = {
        str(item.get("title") or "").strip()
        for item in activities
        if str(item.get("title") or "").strip()
    }
    previous_titles = set(ledger.get("locked_candidate_titles") or [])
    raw = parsed.get("plan_delta")
    raw = raw if isinstance(raw, dict) else {}

    def titles(field: str, allowed: set[str]) -> list[str]:
        return list(dict.fromkeys(
            text
            for value in (raw.get(field) or [])
            if (text := str(value).strip()) in allowed
        ))

    preserve = titles("preserve", known_titles | previous_titles)
    add = titles("add", known_titles)
    remove = titles("remove", previous_titles)
    if not raw:
        preserve = [
            str(item.get("title") or "")
            for item in activities
            if item.get("origin") == "baseline"
        ]
        add = [
            str(item.get("title") or "")
            for item in activities
            if item.get("origin") == "current_research"
        ]
    return {
        "preserve": preserve,
        "add": add,
        "remove": remove,
        "replace": [
            value
            for value in (raw.get("replace") or [])
            if isinstance(value, dict)
        ][:10],
        "unresolved": [
            str(value).strip()
            for value in (raw.get("unresolved") or [])
            if str(value).strip()
        ][:5],
    }


def _ensure_subgoal_target_coverage(
    itinerary: list[dict],
    fallback_itinerary: list[dict],
    *,
    activities: list[dict],
    subgoals: list[dict],
) -> tuple[list[dict], bool]:
    """Repair a model draft that omits valid candidates needed by a subgoal."""
    activity_by_title = {
        str(item.get("title") or "").strip(): item
        for item in activities
        if str(item.get("title") or "").strip()
    }
    result = list(itinerary)
    selected_titles = {
        str(item.get("candidate_title") or "").strip()
        for item in result
        if str(item.get("candidate_title") or "").strip()
    }
    repaired = False
    for subgoal in subgoals:
        subgoal_id = str(subgoal.get("id") or "").strip()
        if not subgoal_id or subgoal.get("required") is False:
            continue
        try:
            target_count = int(subgoal.get("target_count") or 1)
        except (TypeError, ValueError):
            target_count = 1
        target_count = max(1, min(20, target_count))
        observed = sum(
            1
            for item in result
            if subgoal_id in (
                activity_by_title.get(
                    str(item.get("candidate_title") or "").strip(),
                    {},
                ).get("subgoal_ids")
                or []
            )
        )
        if observed >= target_count:
            continue
        for fallback_item in fallback_itinerary:
            title = str(
                fallback_item.get("candidate_title") or ""
            ).strip()
            activity = activity_by_title.get(title)
            if (
                not activity
                or title in selected_titles
                or subgoal_id not in (activity.get("subgoal_ids") or [])
            ):
                continue
            result.append(dict(fallback_item))
            selected_titles.add(title)
            observed += 1
            repaired = True
            if observed >= target_count:
                break
    return result[:20], repaired


def _evidence_label(status: str | None) -> str:
    """证据状态 → 回复正文内联的中文标注（与卡片六态一致）。"""
    return {
        "official_source_confirmed": "官方确认",
        "public_source_observed": "公开来源待核实",
        "confirmed_by_user": "已确认",
        "estimated": "估算",
        "expired": "已过期",
    }.get(str(status or "").strip(), "待核实")


def _inline_schedule(itinerary: list[dict], activities: list[dict]) -> str:
    """把行程逐项内联成自洽文本：日/时段 标题（地点・理由・证据）。

    让聊天正文离开卡片也能看懂完整行程（产品目标：卡片仅为可选辅助）。
    """
    by_title = {
        str(item.get("title") or "").strip(): item
        for item in activities
        if str(item.get("title") or "").strip()
    }
    lines: list[str] = []
    for item in itinerary:
        title = str(item.get("candidate_title") or "").strip()
        if not title:
            continue
        activity = by_title.get(title, {})
        day = str(item.get("day") or "周末").strip()
        window = str(item.get("time_window") or "待确认").strip()
        venue = str(activity.get("venue") or "").strip()
        reason = str(item.get("reason") or "").strip()
        detail = title + (f"（{venue}）" if venue else "")
        tail = "；".join(
            part for part in (reason, _evidence_label(activity.get("verification_status")))
            if part
        )
        lines.append(f"{day} {window} {detail}" + (f"——{tail}" if tail else ""))
    return "\n".join(f"· {line}" for line in lines)


def _repaired_reply(itinerary: list[dict], activities: list[dict]) -> str:
    by_title = {
        str(item.get("title") or "").strip(): item
        for item in activities
        if str(item.get("title") or "").strip()
    }
    preserved: list[str] = []
    added: list[str] = []
    for item in itinerary:
        title = str(item.get("candidate_title") or "").strip()
        if not title:
            continue
        target = (
            preserved
            if by_title.get(title, {}).get("origin") == "baseline"
            else added
        )
        if title not in target:
            target.append(title)
    parts = ["我已根据当前对话更新同一份行程"]
    if preserved:
        parts.append(f"保留了{'、'.join(preserved)}")
    if added:
        parts.append(f"新增并安排了{'、'.join(added)}")
    header = "：".join([parts[0], "；".join(parts[1:])]) + "。"
    # 不再把细节推给卡片：直接内联完整排期，聊天正文自洽。
    schedule = _inline_schedule(itinerary, activities)
    return header + (f"具体安排如下：\n{schedule}" if schedule else "")


def _fallback_response(state: dict, activities: list[dict]) -> dict:
    research = dict(state.get("research") or {})
    constraints = dict(state.get("constraints") or {})
    artifacts = list(state.get("research_artifacts") or [])
    raw_candidates = list(state.get("research_raw_candidates") or [])
    judged_candidates = list(state.get("research_judged_candidates") or [])
    selection = dict(state.get("research_selection") or {})
    subgoals = list(constraints.get("research_subgoals") or [])
    names = [
        str(item.get("title") or "")
        for item in activities[:5]
        if str(item.get("title") or "").strip()
    ]
    if research.get("provider_status") == "unavailable":
        reply = (
            "我保留了上一轮已有方案，但这次外部搜索服务不可用，"
            "因此还不能可靠确认新增目标；这不代表没有符合条件的结果。"
        )
    elif state.get("research_outcome") == "partial_unverified" and names:
        reply = (
            f"我保留了原行程，并加入了本轮找到的来源支持候选。"
            f"目前共有 {len(activities)} 项，包括{'、'.join(names[:3])}；"
            "部分候选的语义或营业信息仍待复核，我已保留证据，不会把它们误报成已确认。"
        )
    elif names:
        reply = (
            f"我结合当前对话和本轮研究，先保留了 {len(activities)} 个"
            f"有来源的候选，包括{'、'.join(names[:3])}。"
            "我会把它们编排进同一份行程继续调整。"
        )
    else:
        reply = (
            "本轮没有得到足够的来源证据，我暂时不编造具体安排。"
            "可以继续补充偏好，或在外部检索恢复后重试。"
        )
    itinerary_candidates = []
    selected_titles: set[str] = set()
    for subgoal in subgoals:
        subgoal_id = str(subgoal.get("id") or "")
        try:
            target_count = int(subgoal.get("target_count") or 1)
        except (TypeError, ValueError):
            target_count = 1
        target_count = max(1, min(20, target_count))
        matches = [
            item
            for item in activities
            if subgoal_id in (item.get("subgoal_ids") or [])
            and str(item.get("title") or "") not in selected_titles
        ][:target_count]
        for match in matches:
            itinerary_candidates.append(match)
            selected_titles.add(str(match.get("title") or ""))
    if not itinerary_candidates:
        for item in activities[:8]:
            title = str(item.get("title") or "")
            if title and title not in selected_titles:
                itinerary_candidates.append(item)
                selected_titles.add(title)
    itinerary = [
        {
            "day": "周末",
            "time_window": "待结合开放时间安排",
            "candidate_title": item.get("title"),
            "reason": "来源支持的当前候选",
        }
        for item in itinerary_candidates[:12]
    ]
    # 把排期内联进回复正文，使聊天离开卡片也自洽（provider 不可用/无候选时不附加）。
    if names and research.get("provider_status") != "unavailable":
        schedule = _inline_schedule(itinerary, activities)
        if schedule:
            reply = f"{reply}\n当前安排如下：\n{schedule}"
    semantic_evaluation = state.get("research_semantic_evaluation") or {}
    if "missing_subgoal_ids" in semantic_evaluation:
        missing_subgoal_ids = list(
            semantic_evaluation.get("missing_subgoal_ids") or []
        )
    else:
        missing_subgoal_ids = [
            item.get("id") for item in subgoals if item.get("id")
        ]
    ledger = _plan_ledger(state, activities)
    delta = _validated_plan_delta({}, activities=activities, ledger=ledger)
    return {
        "assistant_response": reply,
        "itinerary_draft": itinerary,
        "plan_ledger": ledger,
        "plan_delta": delta,
        "research_context": {
            "goal": constraints.get("research_goal"),
            "status": research.get("status"),
            "provider_status": research.get("provider_status"),
            "summary": (
                artifacts[-1].get("summary")
                if artifacts and isinstance(artifacts[-1], dict)
                else reply
            ),
            "candidate_titles": names,
            "raw_candidate_count": len(raw_candidates),
            "raw_candidate_summaries": [
                _compact_activity(item) for item in raw_candidates[:30]
            ],
            "judged_candidate_summaries": [
                _compact_activity(item) for item in judged_candidates[:30]
            ],
            "selection": selection,
            "covered_subgoal_ids": list(
                semantic_evaluation.get("covered_subgoal_ids")
                or []
            ),
            "missing_subgoal_ids": missing_subgoal_ids,
        },
    }


def compose_research_response(state: dict) -> dict:
    """Generate one evidence-grounded response and a preliminary itinerary."""
    activities = list(state.get("activities") or [])
    fallback = _fallback_response(state, activities)
    constraints = dict(state.get("constraints") or {})
    raw_candidates = list(state.get("research_raw_candidates") or [])
    judged_candidates = list(state.get("research_judged_candidates") or [])
    selection = dict(state.get("research_selection") or {})
    ledger = _plan_ledger(state, activities)
    conversation_memory = _conversation_memory(
        list(state.get("conversation") or [])
    )
    payload = {
        "conversation_memory": conversation_memory,
        "plan_ledger": ledger,
        "constraints": constraints,
        "revision_mode": state.get("research_revision_mode") or "initial",
        "current_itinerary": list(state.get("itinerary_draft") or [])[:12],
        "previous_assistant_response": state.get("assistant_response"),
        "research": {
            key: value
            for key, value in dict(state.get("research") or {}).items()
            if key not in {"trace"}
        },
        "research_artifacts": list(state.get("research_artifacts") or [])[-6:],
        "semantic_evaluation": state.get("research_semantic_evaluation") or {},
        "research_selection": selection,
        "raw_research_candidates": [
            _compact_activity(item) for item in raw_candidates[:30]
        ],
        "judged_research_candidates": [
            _compact_activity(item) for item in judged_candidates[:30]
        ],
        "current_candidates": [_compact_activity(item) for item in activities[:30]],
        "previous_plan_candidates": [
            _compact_activity(item)
            for item in (state.get("research_baseline_activities") or [])[:10]
        ],
        "weather": state.get("weather") or {},
        "replan_reason": state.get("replan_reason"),
    }
    parsed = extract_json(
        "trip_response_compose",
        """You are the conversational planning layer of a complete travel agent.
The chat reply is a standalone interface: it must be self-contained so the user
can understand the whole plan from the reply text alone. Cards below are an
optional redundant view, never a substitute. NEVER write pointer phrases such as
"见下方/见卡片/如下卡片/详情见下方" — inline the content instead.

Continue the same conversation naturally using the full functional memory
(recent turns, earlier-turn ledger, goals, existing itinerary, current
candidates, research artifacts). Directly answer the user's latest request; do
not merely report that cards were found. For every scheduled item, state inline
its day, time window, place/venue, a one-line reason, and its evidence status
(官方确认 / 公开来源待核实 / 估算). When this is a revision, preserve unaffected
choices and explain only material changes; never describe a revision as a first
plan.

Weather: if `weather.adverse` is true, or `replan_reason` or the latest user
turn raises rain/storm/typhoon concerns, address it head-on first, prefer indoor
arrangements, and explicitly flag the weather risk of any outdoor item. Do NOT
silently add outdoor/open-air/water activities in a rainy context.

Never claim that no option exists when provider_status is unavailable.
Never invent a place, event, date, address, opening time, price, or source.
Only schedule candidate_title values present in current_candidates. A draft may
use a broad time window when exact hours are unknown. If an objective remains
uncovered, say why and ask at most one useful follow-up question.

Return:
{"reply":"self-contained natural response with the full inline itinerary",
 "itinerary_draft":[
  {"day":"周六/周日/周末","time_window":"上午/下午/晚上/待确认",
   "candidate_title":"exact title from current_candidates",
   "reason":"short evidence-grounded reason"}
 ],
 "plan_delta":{"preserve":["existing title"],"add":["new title"],
  "remove":[],"replace":[],"unresolved":[]},
 "unresolved_questions":["at most one question"],
 "research_summary":"short durable summary"}""",
        json.dumps(payload, ensure_ascii=False, default=str),
        timeout=get_settings().trip_response_compose_timeout_s,
    )
    if not isinstance(parsed, dict) or not str(parsed.get("reply") or "").strip():
        return fallback
    known_titles = {
        str(item.get("title") or "").strip()
        for item in activities
        if str(item.get("title") or "").strip()
    }
    itinerary = []
    for raw in parsed.get("itinerary_draft") or []:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("candidate_title") or "").strip()
        if title not in known_titles:
            continue
        itinerary.append({
            "day": str(raw.get("day") or "周末").strip(),
            "time_window": str(raw.get("time_window") or "待确认").strip(),
            "candidate_title": title,
            "reason": str(raw.get("reason") or "").strip(),
        })
    itinerary, itinerary_repaired = _ensure_subgoal_target_coverage(
        itinerary,
        list(fallback.get("itinerary_draft") or []),
        activities=activities,
        subgoals=list(constraints.get("research_subgoals") or []),
    )
    context = dict(fallback["research_context"])
    context["summary"] = (
        str(parsed.get("research_summary") or "").strip()
        or context.get("summary")
    )
    context.update({
        "raw_candidate_count": len(raw_candidates),
        "raw_candidate_summaries": [
            _compact_activity(item) for item in raw_candidates[:30]
        ],
        "judged_candidate_summaries": [
            _compact_activity(item) for item in judged_candidates[:30]
        ],
        "selection": selection,
    })
    delta = _validated_plan_delta(
        parsed,
        activities=activities,
        ledger=ledger,
    )
    next_ledger = {
        **ledger,
        "last_plan_delta": delta,
        "selected_candidate_titles": [
            str(item.get("title") or "").strip()
            for item in activities
            if str(item.get("title") or "").strip()
        ],
    }
    return {
        "assistant_response": (
            _repaired_reply(itinerary, activities)
            if itinerary_repaired
            else str(parsed["reply"]).strip()
        ),
        "itinerary_draft": itinerary or fallback["itinerary_draft"],
        "plan_ledger": next_ledger,
        "plan_delta": delta,
        "unresolved_questions": [
            str(value).strip()
            for value in (parsed.get("unresolved_questions") or [])[:1]
            if str(value).strip()
        ],
        "research_context": context,
    }


def _slot_time_label(iso: str | None) -> str:
    """ISO → Asia/Shanghai 的 MM-DD HH:MM（零点只显日期）；解析失败返回空串。"""
    if not iso:
        return ""
    try:
        from datetime import datetime
        from ..config import SHANGHAI_TZ

        dt = datetime.fromisoformat(str(iso))
        if dt.tzinfo is not None:
            dt = dt.astimezone(SHANGHAI_TZ)
        return dt.strftime("%m-%d") if (dt.hour == 0 and dt.minute == 0) else dt.strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return ""


def compose_confirm_reply(state: dict) -> str:
    """确认版自洽叙述：基于已排定 timeline（含精确时间）+ 天气 + 回填。

    纯模板、不调 LLM（离线可测）；聊天正文离开卡片也能看懂完整确认版。
    """
    timeline = list(state.get("timeline") or [])
    weather = dict(state.get("weather") or {})
    _KIND_LABEL = {
        "transport": "交通", "activity": "活动", "dining": "用餐",
        "hotel": "住宿", "buffer": "缓冲", "checkin": "入住", "checkout": "退房",
    }
    lines: list[str] = []
    for slot in timeline:
        title = str(slot.get("title") or "").strip()
        if not title:
            continue
        label = _KIND_LABEL.get(str(slot.get("kind") or ""), "安排")
        when = _slot_time_label(slot.get("start_at"))
        end = _slot_time_label(slot.get("end_at"))
        span = when + (f"~{end[-5:]}" if end and when else "")
        lines.append(f"{span} 【{label}】{title}".strip())
    if not lines:
        return ""
    head = "已为你生成确认版行程，逐项时间安排如下："
    if weather.get("adverse"):
        detail = str(weather.get("detail") or "恶劣天气").strip()
        head = (
            f"考虑到{detail}，我已优先安排室内项并标注户外风险；确认版逐项时间安排如下："
        )
    booked = [
        str((b.get("extracted") or {}).get("train_no")
            or (b.get("extracted") or {}).get("flight_no")
            or (b.get("extracted") or {}).get("name") or "").strip()
        for b in (state.get("bookings") or [])
        if b.get("confirmed")
    ]
    booked = [b for b in booked if b]
    tail = f"\n已确认回填：{'、'.join(booked)}。" if booked else ""
    return head + "\n" + "\n".join(f"· {line}" for line in lines) + tail
