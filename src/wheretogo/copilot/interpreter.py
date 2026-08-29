"""上下文感知的单轮解释器。

主路径让 LLM 输出受 schema 约束的语义结构；确定性规则只负责安全兜底、格式归一和
高精度保护。关键词不再定义完整语言空间，它们只是模型不可用时的降级能力。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..providers import extract_json
from .nlu import (
    _normalize_llm_result,
    extract_constraints_from_text,
)
from .turn_schema import ConstraintOperation, TurnCommand, TurnDecision

_VALID_INTENTS = {
    "provide_constraints",
    "clarify_answer",
    "refine_field",
    "deep_research",
    "confirm_booking",
    "ask_info",
    "weather",
    "chitchat",
}
_VALID_ACTS = {
    "update_constraints",
    "research_more",
    "recompose_plan",
    "answer_info",
    "submit_booking",
    "weather_replan",
    "chitchat",
    "clarify",
}
# v4：封闭的可执行能力类型（动作空间有限，目标语义保持开放）
_VALID_ACTION_TYPES = {
    "research",
    "transport_search",
    "compose_itinerary",
    "answer",
    "booking",
    "replan",
}
_CONSTRAINT_KEYS = {
    "origins",
    "target_city_name",
    "target_city_code",
    "party_size",
    "budget_band",
    "interests",
    "soft_preferences",
    "experience_requirements",
    "research_goal",
    "acceptance_criteria",
    "research_subgoals",
    "dietary",
    "weekend_start",
    "weekend_end",
    "latest_return",
}
_CITY_ONLY = re.compile(r"^[\s，。！？,.!?]*(?:我(?:们)?从)?([\u4e00-\u9fff]{2,8})(?:出发)?[\s，。！？,.!?]*$")
_NO_EXTERNAL_RESEARCH = re.compile(
    r"(?:不要|不用|无需|别|不必).{0,8}(?:重新)?(?:搜索|检索|调研|查找|查)"
    r"|(?:do\s+not|don't|without)\s+(?:re)?search",
    re.IGNORECASE,
)


def _recent_context(conversation: list[dict] | None, limit: int = 24) -> list[dict]:
    out = []
    for turn in (conversation or [])[-limit:]:
        role = str(turn.get("role") or "")
        content = str(turn.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            item = {"role": role, "content": content[:1600]}
            research_context = turn.get("research_context")
            if isinstance(research_context, dict):
                item["research_context"] = {
                    "goal": research_context.get("goal"),
                    "status": research_context.get("status"),
                    "summary": research_context.get("summary"),
                    "candidate_titles": list(
                        research_context.get("candidate_titles") or []
                    )[:12],
                    "covered_subgoal_ids": list(
                        research_context.get("covered_subgoal_ids") or []
                    )[:12],
                    "missing_subgoal_ids": list(
                        research_context.get("missing_subgoal_ids") or []
                    )[:12],
                }
            out.append(item)
    return out


def _conversation_memory(conversation: list[dict] | None) -> dict:
    """Return recent dialogue plus a bounded chronological long-term ledger."""
    turns = [
        turn
        for turn in (conversation or [])
        if turn.get("role") in {"user", "assistant"}
        and str(turn.get("content") or "").strip()
    ]
    recent_count = min(24, len(turns))
    earlier = turns[:-recent_count] if recent_count else turns
    return {
        "total_turns": len(turns),
        "recent_turns": _recent_context(turns, limit=24),
        "earlier_turn_ledger": [
            {
                "role": str(turn.get("role")),
                "content": str(turn.get("content") or "")[:500],
                "intent": turn.get("intent"),
            }
            for turn in earlier[-80:]
        ],
    }


def _normalize_itinerary_draft(
    values: Any,
    latest_results: dict | None,
) -> list[dict]:
    """Keep only itinerary rows grounded in a currently known candidate."""
    if not isinstance(values, list):
        return []
    known_titles = {
        str(item.get("title") or "").strip()
        for item in ((latest_results or {}).get("activities") or [])
        if str(item.get("title") or "").strip()
    }
    result: list[dict] = []
    for raw in values[:12]:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("candidate_title") or "").strip()
        if not title or title not in known_titles:
            continue
        result.append({
            "day": str(raw.get("day") or "周末").strip(),
            "time_window": str(raw.get("time_window") or "待确认").strip(),
            "candidate_title": title,
            "reason": str(raw.get("reason") or "").strip(),
        })
    return result


def _subgoal_id(objective: str) -> str:
    digest = hashlib.sha1(objective.strip().encode("utf-8")).hexdigest()[:12]
    return f"goal_{digest}"


def _target_count(value: Any) -> int:
    try:
        return max(1, min(20, int(value or 1)))
    except (TypeError, ValueError):
        return 1


def _normalize_subgoals(values: Any) -> list[dict]:
    result: list[dict] = []
    if not isinstance(values, list):
        return result
    for raw in values[:20]:
        if not isinstance(raw, dict):
            continue
        objective = str(raw.get("objective") or "").strip()
        if not objective:
            continue
        criteria = [
            str(value).strip()
            for value in (raw.get("acceptance_criteria") or [objective])
            if str(value).strip()
        ]
        normalized = {
            "id": str(raw.get("id") or _subgoal_id(objective)).strip(),
            "objective": objective,
            "acceptance_criteria": list(dict.fromkeys(criteria or [objective])),
            "required": raw.get("required") is not False,
        }
        if "target_count" in raw:
            normalized["target_count"] = _target_count(raw.get("target_count"))
        result.append(normalized)
    return result


def _normalize_structured_constraints(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}
    # 模型既可返回对外字段（budget_max/departure_date），也可直接返回内部字段。
    normalized = _normalize_llm_result(raw)
    for key in _CONSTRAINT_KEYS:
        if key in raw and key not in normalized:
            normalized[key] = raw[key]
    for field in (
        "experience_requirements",
        "acceptance_criteria",
        "interests",
        "soft_preferences",
    ):
        if field in normalized:
            values = normalized[field]
            values = values if isinstance(values, list) else [values]
            normalized[field] = list(dict.fromkeys(
                str(value).strip()
                for value in values
                if str(value).strip()
            ))
    if "research_subgoals" in normalized:
        normalized["research_subgoals"] = _normalize_subgoals(
            normalized["research_subgoals"]
        )
    try:
        if normalized.get("party_size") is not None:
            normalized["party_size"] = int(normalized["party_size"])
            if normalized["party_size"] <= 0:
                normalized.pop("party_size", None)
    except (TypeError, ValueError):
        normalized.pop("party_size", None)
    return {key: value for key, value in normalized.items() if key in _CONSTRAINT_KEYS}


def _pending_answer_patch(
    message: str,
    patch: dict,
    pending_clarify: list[dict] | None,
) -> dict:
    """把“上海”这类短回答绑定到系统刚问的槽位，而不是脱离上下文猜测。"""
    if not pending_clarify:
        return patch
    slot = str((pending_clarify[0] or {}).get("slot") or "")
    value = (message or "").strip()
    if slot == "origins":
        match = _CITY_ONLY.match(value)
        if match:
            # The pending question is the authoritative frame for a short
            # answer. Models sometimes understand it in prose/memory_note but
            # only echo existing constraints in their structured patch. Merge
            # the bound origin even when such an echo made ``patch`` non-empty.
            bound = dict(patch)
            bound["origins"] = [match.group(1)]
            # A bare city answered to "where do you depart from?" must not also
            # be treated as a destination change.
            bound.pop("target_city_name", None)
            bound.pop("target_city_code", None)
            return bound
    if slot in {"destination", "target_city_name"}:
        # v4 前置解析的阻塞事实是 destination；短答案必须绑到目的地，
        # 否则回答“上海”也破不了循环追问（真实故障：城市追问死循环）。
        match = _CITY_ONLY.match(value) or re.match(
            r"^[\s，。！？,.!?]*(?:就是|就在|就在|在)?([\u4e00-\u9fff]{2,8}?)(?:市内|市区|本地)",
            value,
        )
        if match:
            bound = dict(patch)
            bound["target_city_name"] = match.group(1)
            bound.pop("origins", None)  # 目的地问题的短答案不是出发地
            return bound
    if slot in {"interests", "experience_requirements"} and value:
        return {
            **patch,
            "experience_requirements": [value],
            "research_goal": value,
            "acceptance_criteria": [value],
        }
    return patch


def _safe_operations(raw: Any) -> list[ConstraintOperation]:
    out: list[ConstraintOperation] = []
    if not isinstance(raw, list):
        return out
    for item in raw[:20]:
        try:
            op = ConstraintOperation.model_validate(item)
        except Exception:
            continue
        if op.field in _CONSTRAINT_KEYS:
            out.append(op)
    return out


def _derive_operations(message: str, patch: dict) -> list[ConstraintOperation]:
    operations: list[ConstraintOperation] = []
    lowered = (message or "").lower()
    for field, value in patch.items():
        if field == "interests" and value == []:
            operations.append(ConstraintOperation(op="clear", field=field))
        elif field == "interests" and any(
            marker in lowered
            for marker in ("不想", "不要", "不看", "排除", "除了", "别推荐")
        ):
            operations.append(ConstraintOperation(op="set", field=field, value=value))
        else:
            operations.append(ConstraintOperation(op="set", field=field, value=value))
    return operations


def _dedupe_json_values(values: list[Any]) -> list[Any]:
    """Deduplicate scalar or structured list values without assuming hashability."""
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        try:
            key = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except (TypeError, ValueError):
            key = repr(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _apply_operations(
    memory_ctx: dict,
    patch: dict,
    operations: list[ConstraintOperation],
) -> dict:
    """把结构化 set/add/remove/clear 落成兼容旧 BFF 的 patch。"""
    out = dict(patch)
    list_fields = {
        "origins",
        "interests",
        "soft_preferences",
        "experience_requirements",
        "acceptance_criteria",
        "research_subgoals",
        "dietary",
    }
    for operation in operations:
        field = operation.field
        current = out.get(field, memory_ctx.get(field))
        if operation.op == "clear":
            out[field] = [] if field in list_fields else None
        elif operation.op == "set":
            out[field] = operation.value
        elif operation.op == "add":
            existing = list(current or []) if field in list_fields else []
            additions = (
                operation.value
                if isinstance(operation.value, list)
                else [operation.value]
            )
            out[field] = _dedupe_json_values([
                *existing,
                *(item for item in additions if item is not None),
            ])
        elif operation.op == "remove" and field in list_fields:
            removals = (
                operation.value
                if isinstance(operation.value, list)
                else [operation.value]
            )
            out[field] = [
                item for item in list(current or [])
                if item not in removals
            ]
    return out


def _acts_for(primary: str, patch: dict) -> list[str]:
    acts: list[str] = []
    if patch:
        acts.append("update_constraints")
    mapping = {
        "deep_research": "research_more",
        "ask_info": "answer_info",
        "confirm_booking": "submit_booking",
        "weather": "weather_replan",
        "chitchat": "chitchat",
        "clarify_answer": "clarify",
        "provide_constraints": "clarify",
    }
    act = mapping.get(primary)
    if act:
        acts.append(act)
    return list(dict.fromkeys(acts))


def _commands_for(
    acts: list[str],
    patch: dict,
    message: str,
    pending_clarify: list[dict] | None,
) -> list[TurnCommand]:
    commands: list[TurnCommand] = []
    if "update_constraints" in acts and patch:
        commands.append(TurnCommand(type="update_constraints", payload={"patch": patch}))
    if "research_more" in acts:
        commands.append(TurnCommand(type="research_more", payload={"feedback": message}))
    if "recompose_plan" in acts:
        commands.append(
            TurnCommand(type="recompose_plan", payload={"instruction": message})
        )
    if "answer_info" in acts:
        commands.append(TurnCommand(type="answer", payload={"question": message}))
    if "submit_booking" in acts:
        commands.append(TurnCommand(type="submit_booking_draft", payload={"raw": message}))
    if "weather_replan" in acts:
        commands.append(
            TurnCommand(type="request_weather_replan", payload={"reason": message})
        )
    if "clarify" in acts and pending_clarify:
        commands.append(
            TurnCommand(
                type="ask_clarification",
                payload={"pending": list(pending_clarify)[:4]},
            )
        )
    return commands


def _normalize_goals(values: Any) -> list[dict]:
    """v4 goals：{id, objective, required}；objective 为自由文本。"""
    result: list[dict] = []
    if not isinstance(values, list):
        return result
    for raw in values[:20]:
        if not isinstance(raw, dict):
            continue
        objective = str(raw.get("objective") or "").strip()
        if not objective:
            continue
        result.append({
            "id": str(raw.get("id") or _subgoal_id(objective)).strip(),
            "objective": objective,
            "required": raw.get("required") is not False,
        })
    return result


def _normalize_proposed_actions(values: Any) -> list[dict]:
    """v4 proposed_actions：只接受封闭能力类型；reason 保留自由文本。"""
    result: list[dict] = []
    if not isinstance(values, list):
        return result
    for raw in values[:10]:
        if not isinstance(raw, dict):
            continue
        action_type = str(raw.get("type") or "").strip()
        if action_type not in _VALID_ACTION_TYPES:
            continue
        result.append({
            "type": action_type,
            "reason": str(raw.get("reason") or "").strip()[:300],
        })
    return result


def _normalize_clarification_candidates(values: Any) -> list[dict]:
    """v4 clarification_candidates：{fact, reason}；阻塞与否由运行时判定。"""
    result: list[dict] = []
    if not isinstance(values, list):
        return result
    for raw in values[:8]:
        if not isinstance(raw, dict):
            continue
        fact = str(raw.get("fact") or raw.get("name") or "").strip()
        if not fact:
            continue
        result.append({
            "fact": fact[:80],
            "reason": str(raw.get("reason") or "").strip()[:300],
        })
    return result


def _actions_from_acts(
    acts: list[str],
    primary: str,
    patch: dict,
    latest_results: dict | None,
) -> list[dict]:
    """模型缺失/未输出时的确定性动作推导：只看控制信号，不看话题词。"""
    actions: list[dict] = []
    if "research_more" in acts:
        actions.append({"type": "research", "reason": "用户要求继续/重新研究"})
    if "recompose_plan" in acts:
        actions.append({"type": "compose_itinerary", "reason": "基于已有候选重排"})
    if "answer_info" in acts:
        actions.append({"type": "answer", "reason": "回答用户问题"})
    if "submit_booking" in acts:
        actions.append({"type": "booking", "reason": "识别到订单回填"})
    if "weather_replan" in acts:
        actions.append({"type": "replan", "reason": "天气变化请求重规划"})
    has_results = bool(
        (latest_results or {}).get("activities")
        or (latest_results or {}).get("research_context")
    )
    if not actions:
        # 首轮提供出行约束且尚无研究结果 → 主动研究（AI 主动推荐而非等齐全部信息）
        if primary in {"provide_constraints", "clarify_answer", "refine_field"} and (
            patch or not has_results
        ):
            actions.append({"type": "research", "reason": "按当前约束开展活动调研"})
        elif primary in {"ask_info", "chitchat"}:
            actions.append({"type": "answer", "reason": "直接回答"})
    return actions


def interpret_turn(
    message: str,
    *,
    fallback_intent: str,
    memory_ctx: dict | None = None,
    conversation: list[dict] | None = None,
    stage: str | None = None,
    pending_clarify: list[dict] | None = None,
    latest_results: dict | None = None,
    active_run: dict | None = None,
    use_llm: bool = True,
) -> TurnDecision:
    """把开放语言解释成有限、可验证、可组合的系统动作。"""
    memory_ctx = dict(memory_ctx or {})
    conversation_memory = _conversation_memory(conversation)
    deterministic = extract_constraints_from_text(message, use_llm=False)
    parsed: dict | None = None
    if use_llm:
        system_prompt = """你是周末旅行 Agent 的 turn interpreter。只输出 JSON，不执行动作。
用户的一句话可以同时包含多个动作，不能强迫只选一个。

JSON:
{
  "primary_intent": "provide_constraints|clarify_answer|refine_field|deep_research|confirm_booking|ask_info|weather|chitchat",
  "acts": ["update_constraints|research_more|recompose_plan|answer_info|submit_booking|weather_replan|chitchat|clarify"],
  "constraints": {},
  "constraint_operations": [{"op":"set|add|remove|clear","field":"...","value":null}],
  "research_goal": "用完整自然语言表述本轮真正要解决的问题，若无则 null",
  "acceptance_criteria": ["候选必须满足或核实的开放文本标准"],
  "references": ["被当前消息指代的历史对象"],
  "goals": [{"id":"稳定id","objective":"本轮要完成的一个目标","required":true}],
  "proposed_actions": [{"type":"research|transport_search|compose_itinerary|answer|booking|replan","reason":"为什么需要这个动作"}],
  "clarification_candidates": [{"fact":"缺少的事实名（如 origin/start_location）","reason":"这个事实影响什么"}],
  "clarification": null,
  "assistant_reply": "作为旅行助理直接回复；若要研究，只说明下一步，不假装已有结果",
  "itinerary_draft": [
    {"day":"周六","time_window":"上午","candidate_title":"已有候选的精确标题","reason":"安排理由"}
  ],
  "memory_note": "供后续长程任务使用的一句事实性进展摘要",
  "confidence": 0.0
}

要求：
1. 利用最近对话、当前约束、阶段和待澄清槽位解析“还是那个/换一批/就上海”等省略表达。
2. 出发地、目的地、日期、人数、预算等稳定旅行属性写入 constraints。
3. 所有体验诉求、偏好和排除项都写入开放字段 experience_requirements，
   不得映射到预定义兴趣类别；同时生成 research_goal 和 acceptance_criteria。
   constraints.research_subgoals 必须保存当前全部有效的开放目标；每项格式为
   {"id":"稳定 id","objective":"用户要完成的一个目标",
    "acceptance_criteria":["只针对一个候选本身的可核实标准"],
    "required":true,"target_count":1}。
   每个可独立保留、核实、增删或替换的对象/目标都应成为独立子目标，不要把整套
   行程的多个对象压成一个候选必须独自完成的目标。target_count 表示这个子目标
   期望获得的不同候选数；用户明确数量时照实填写，未明确时填 1。跨多个候选才能
   判断的路线完整性、先后顺序和整体体验保留在 research_goal 中，由行程编排阶段
   判断，不得作为单个候选的 acceptance_criteria。
   “还想/另外/同时”表示保留旧目标并新增子目标；“改成/不想/不要了”才替换或删除。
   acceptance_criteria 必须是可由来源核实的成功标准，并明确候选本身应是用户
   可以选择或到访的具体对象；除非用户要的就是资讯或优惠，否则不能用提到该
   对象的新闻、榜单、促销或泛化文章代替对象本身。
4. 每一轮都必须像完整旅行助理一样直接回答用户。询问已有推荐、比较方案、解释理由
   或调整已有行程时，使用 latest_plan_and_research，输出 answer_info 或
   recompose_plan，不要重新搜索。只有当前证据无法回答、核心检索条件变化、用户明确
   要找新选项或核实最新外部事实时，才输出 research_more。
   recompose_plan 可以重排行程，但 itinerary_draft 只能引用已有候选的精确标题。
   如果将执行 research_more，assistant_reply 只能诚实说明准备研究什么。
5. 普通聊天、解释、比较、建议和行程修改也是完整任务，不得退化成固定欢迎语、
   数据库关键词查询，也不得追问上下文里已经存在的信息。
6. 一句话可以同时更新约束、要求继续研究和询问信息。
7. 否定、替换必须结合 current_constraints 输出修改后的最终
   experience_requirements；不确定且会产生副作用时给 clarification。
8. 只抽取消息明确表达或上下文可唯一解析的信息，不臆造事实。
9. assistant_reply 最多问一个问题，并且不得追问
   deterministic_message_constraints 已经解析到的出发地、日期、目的地或人数。
10. goals 把本轮要完成的目标写成自然语言；proposed_actions 只使用列出的能力类型：
   在目的地城市内找/核实活动和地点→research；比较或查询跨城大交通（高铁/航班）
   →transport_search；只用已有候选重排行程→compose_itinerary；直接回答→answer。
   clarification_candidates 只列出缺少的事实及其影响，不要自己决定是否阻塞执行；
   能先做的部分绝不因可选信息缺失而停止。如果 active_run 显示有任务正在执行，
   判断新消息是追加目标、替换目标还是与任务无关的提问，并如实反映在 goals 中。
"""
        context = {
            "current_message": message,
            "deterministic_message_constraints": deterministic,
            "conversation_memory": conversation_memory,
            "current_constraints": memory_ctx,
            "stage": stage,
            "pending_clarify": pending_clarify or [],
            "latest_plan_and_research": latest_results or {},
            "active_run": active_run or None,
        }
        value = extract_json(
            "turn_interpret",
            system_prompt,
            json.dumps(context, ensure_ascii=False),
            timeout=90.0,
        )
        if isinstance(value, dict):
            parsed = value

    if parsed:
        primary = str(parsed.get("primary_intent") or fallback_intent)
        if primary not in _VALID_INTENTS:
            primary = fallback_intent
        model_patch = _normalize_structured_constraints(parsed.get("constraints"))
        patch = {**deterministic, **model_patch}
        source = "hybrid" if deterministic else "llm"
        raw_acts = parsed.get("acts") if isinstance(parsed.get("acts"), list) else []
        acts = [str(item) for item in raw_acts if str(item) in _VALID_ACTS]
        operations = _safe_operations(parsed.get("constraint_operations"))
        references = [
            str(item)[:200]
            for item in (parsed.get("references") or [])
            if str(item).strip()
        ][:10]
        try:
            confidence = float(parsed.get("confidence", 0.75))
        except (TypeError, ValueError):
            confidence = 0.75
        clarification = (
            str(parsed["clarification"]).strip()
            if parsed.get("clarification")
            else None
        )
        research_goal = (
            str(parsed.get("research_goal") or "").strip() or None
        )
        acceptance_criteria = [
            str(value).strip()
            for value in (parsed.get("acceptance_criteria") or [])
            if str(value).strip()
        ]
        assistant_reply = (
            str(parsed.get("assistant_reply") or "").strip() or None
        )
        itinerary_draft = _normalize_itinerary_draft(
            parsed.get("itinerary_draft"),
            latest_results,
        )
        memory_note = str(parsed.get("memory_note") or "").strip() or None
        goals = _normalize_goals(parsed.get("goals"))
        proposed_actions = _normalize_proposed_actions(parsed.get("proposed_actions"))
        clarification_candidates = _normalize_clarification_candidates(
            parsed.get("clarification_candidates")
        )
    else:
        primary = fallback_intent
        patch = deterministic
        source = "rules"
        acts = []
        operations = []
        references = []
        confidence = 0.55
        clarification = None
        research_goal = None
        acceptance_criteria = []
        assistant_reply = None
        itinerary_draft = []
        memory_note = None
        goals = []
        proposed_actions = []
        clarification_candidates = []
        if latest_results and _NO_EXTERNAL_RESEARCH.search(message):
            # Tool-permission guard: on model failure, an explicit instruction
            # not to search must win over heuristic intent routing. Preserve the
            # current plan instead of mutating dates/preferences or invoking I/O.
            primary = "ask_info"
            patch = {}
            acts = ["recompose_plan"]
            assistant_reply = (
                "我会保留现有候选且不启动外部搜索。"
                "这轮语义规划暂时没能可靠完成重排，所以尚未改动行程；请再试一次。"
            )
            itinerary_draft = list(
                (latest_results or {}).get("itinerary_draft") or []
            )[:12]
        # During a model outage, preserve the user's open wording instead of
        # pretending a keyword taxonomy understood it.  The next research run
        # can still plan from the verbatim goal; strict semantic validation
        # remains unavailable and therefore cannot falsely pass.
        if (
            not acts
            and primary in {"refine_field", "deep_research"}
            and message.strip()
        ):
            research_goal = message.strip()
            acceptance_criteria = [message.strip()]
            if primary == "refine_field":
                patch["experience_requirements"] = [message.strip()]
        elif primary == "provide_constraints" and patch and not pending_clarify:
            research_goal = message.strip() or None

    patch = _pending_answer_patch(message, patch, pending_clarify)
    if research_goal:
        patch["research_goal"] = research_goal
    if acceptance_criteria:
        patch["acceptance_criteria"] = list(dict.fromkeys(acceptance_criteria))
    # 确定性高精度识别补充 acts；模型不能因为单标签而丢掉可并行动作。
    acts = list(dict.fromkeys([*acts, *_acts_for(primary, patch)]))
    if not operations:
        operations = _derive_operations(message, patch)
    else:
        patch = _apply_operations(memory_ctx, patch, operations)
        # Operation payloads are model output too.  A valid ``set`` may carry a
        # scalar for a list-valued constraint; normalize again after applying
        # operations so downstream state never sees a string as an iterable of
        # characters.
        patch = _normalize_structured_constraints(patch)
        acts = list(dict.fromkeys([*acts, *_acts_for(primary, patch)]))
    if "research_subgoals" in patch:
        patch["research_subgoals"] = _normalize_subgoals(
            patch.get("research_subgoals")
        )
    elif "experience_requirements" in patch:
        patch["research_subgoals"] = [
            {
                "id": _subgoal_id(str(requirement)),
                "objective": str(requirement).strip(),
                "acceptance_criteria": [str(requirement).strip()],
                "required": True,
                "target_count": 1,
            }
            for requirement in (patch.get("experience_requirements") or [])
            if str(requirement).strip()
        ]
    commands = _commands_for(acts, patch, message, pending_clarify)
    # v4：动作提案缺失时从 acts 确定性推导；目标缺失时从子目标/研究目标补齐。
    if not proposed_actions:
        proposed_actions = _actions_from_acts(acts, primary, patch, latest_results)
    if not goals:
        goals = _normalize_goals([
            {"id": sub.get("id"), "objective": sub.get("objective"), "required": sub.get("required", True)}
            for sub in (patch.get("research_subgoals") or [])
        ]) or (
            [{"id": _subgoal_id(research_goal), "objective": research_goal, "required": True}]
            if research_goal else []
        )
    return TurnDecision(
        primary_intent=primary,
        acts=acts,
        constraints_patch=patch,
        constraint_operations=operations,
        commands=commands,
        references=references,
        research_goal=research_goal,
        acceptance_criteria=acceptance_criteria,
        clarification=clarification,
        assistant_reply=assistant_reply,
        itinerary_draft=itinerary_draft,
        memory_note=memory_note,
        confidence=confidence,
        interpretation_source=source,
        goals=goals,
        proposed_actions=proposed_actions,
        clarification_candidates=clarification_candidates,
    )
