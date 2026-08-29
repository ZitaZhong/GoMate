"""Open-world semantics for research planning and candidate evaluation.

Only the agent's executable capabilities are closed and typed.  User goals,
requirements, candidate kinds, and evidence criteria remain free-form text.
There is deliberately no catalogue of travel interests in this module.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

from ..config import get_settings
from ..providers import extract_json


RESEARCH_TOOLS: dict[str, str] = {
    "web_search": "Search the open web for current, source-backed information.",
    "map_places": "Search a map/POI provider for named places and locations.",
}


def _texts(values: Any) -> list[str]:
    raw = values if isinstance(values, list) else [values]
    result: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            value = item.get("text") or item.get("requirement") or item.get("value")
        else:
            value = item
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _optional_iso_datetime(value: Any) -> str | None:
    """Keep only parseable ISO dates emitted by the model."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.lower() in {"none", "null", "unknown", "n/a"}:
        return None
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return text


def _target_count(value: Any) -> int:
    try:
        return max(1, min(20, int(value or 1)))
    except (TypeError, ValueError):
        return 1


def _same_criterion(expected: str, observed: str) -> bool:
    """Compare model-returned criteria without introducing a domain taxonomy."""
    left = "".join(str(expected or "").lower().split())
    right = "".join(str(observed or "").lower().split())
    if not left or not right:
        return False
    return left == right or (
        min(len(left), len(right)) >= 4
        and (left in right or right in left)
    )


def requirements_from_constraints(constraints: dict | None) -> list[str]:
    """Return open requirements, with legacy fields as a read-only migration path."""
    value = dict(constraints or {})
    requirements = _texts(value.get("experience_requirements"))
    if requirements:
        return requirements
    # Old checkpoints may still contain these fields.  Their values are treated
    # as verbatim text; they are never normalized through a domain taxonomy.
    return _texts(value.get("interests")) + [
        item
        for item in _texts(value.get("soft_preferences"))
        if item not in _texts(value.get("interests"))
    ]


def research_goal_from_constraints(
    constraints: dict | None,
    feedback: str | None = None,
) -> str:
    value = dict(constraints or {})
    explicit = str(value.get("research_goal") or "").strip()
    if feedback and str(feedback).strip():
        return str(feedback).strip()
    if explicit:
        return explicit
    requirements = requirements_from_constraints(value)
    return "；".join(requirements)


def acceptance_criteria_from_constraints(constraints: dict | None) -> list[str]:
    value = dict(constraints or {})
    explicit = _texts(value.get("acceptance_criteria"))
    if explicit:
        return explicit
    return requirements_from_constraints(value)


def research_subgoals_from_constraints(constraints: dict | None) -> list[dict]:
    """Return open-text plan objectives without mapping them to a taxonomy."""
    value = dict(constraints or {})
    result: list[dict] = []
    for index, raw in enumerate(value.get("research_subgoals") or []):
        if not isinstance(raw, dict):
            continue
        # ``requirement`` was used by an early checkpoint prototype.  Accept it
        # as a read-only migration alias while emitting only ``objective``.
        objective = str(
            raw.get("objective") or raw.get("requirement") or ""
        ).strip()
        if not objective:
            continue
        subgoal_id = str(raw.get("id") or f"goal_{index + 1}").strip()
        criteria = _texts(raw.get("acceptance_criteria")) or [objective]
        result.append({
            "id": subgoal_id,
            "objective": objective,
            "acceptance_criteria": criteria,
            "required": raw.get("required") is not False,
            "target_count": _target_count(raw.get("target_count")),
        })
    if result:
        return result
    requirements = requirements_from_constraints(value)
    if requirements:
        return [
            {
                "id": f"goal_{index + 1}",
                "objective": requirement,
                "acceptance_criteria": [requirement],
                "required": True,
                "target_count": 1,
            }
            for index, requirement in enumerate(requirements)
        ]
    goal = research_goal_from_constraints(value)
    if not goal:
        return []
    return [{
        "id": "goal_1",
        "objective": goal,
        "acceptance_criteria": acceptance_criteria_from_constraints(value) or [goal],
        "required": True,
        "target_count": 1,
    }]


def plan_research_tasks(brief: dict, max_tasks: int = 6) -> list[dict]:
    """Let the model plan free-form research tasks over a finite tool registry."""
    goal = str(brief.get("research_goal") or brief.get("nl_query") or "").strip()
    criteria = _texts(brief.get("acceptance_criteria"))
    gaps = _texts(brief.get("research_gaps") or brief.get("follow_up_queries"))
    city = str(brief.get("city_name") or brief.get("city_code") or "").strip()
    weekend = brief.get("weekend") or {}
    subgoals = research_subgoals_from_constraints(brief)
    supplied_tools = brief.get("available_tools")
    available_tools = (
        {
            str(name): str(description)
            for name, description in supplied_tools.items()
            if str(name) in RESEARCH_TOOLS
        }
        if isinstance(supplied_tools, dict)
        else dict(RESEARCH_TOOLS)
    )
    if not available_tools:
        return []
    parsed = extract_json(
        "research_task_plan",
        """You are the research supervisor. Create diverse, non-overlapping tasks
that can satisfy the user's goal. Choose only from the supplied tool registry.
Do not classify the topic into a predefined travel category.
When research gaps are supplied, plan how to close them instead of blindly
turning every gap into a web-search task.

Return:
{"tasks":[{"query":"free-form search query","tool":"tool name",
"purpose":"what evidence this task should find",
"subgoal_ids":["goal_1"]}]}
Every required subgoal must be covered by at least one task when task budget allows.""",
        json.dumps(
            {
                "goal": goal,
                "acceptance_criteria": criteria,
                "research_subgoals": subgoals,
                "research_gaps": gaps,
                "city": city,
                "time_window": weekend,
                "available_tools": available_tools,
            },
            ensure_ascii=False,
        ),
    )
    tasks: list[dict] = []
    if isinstance(parsed, dict):
        for raw in parsed.get("tasks") or []:
            if not isinstance(raw, dict):
                continue
            query = str(raw.get("query") or "").strip()
            tool = str(raw.get("tool") or "").strip()
            purpose = str(raw.get("purpose") or "").strip()
            subgoal_ids = [
                str(value).strip()
                for value in (raw.get("subgoal_ids") or [])
                if str(value).strip()
            ]
            if query and tool in available_tools:
                tasks.append({
                    "query": query,
                    "tool": tool,
                    "purpose": purpose,
                    "subgoal_ids": subgoal_ids,
                })
    if tasks:
        if subgoals and not any(task.get("subgoal_ids") for task in tasks):
            for index, task in enumerate(tasks):
                task["subgoal_ids"] = [
                    subgoals[index % len(subgoals)]["id"]
                ]
        covered = {
            subgoal_id
            for task in tasks
            for subgoal_id in task.get("subgoal_ids") or []
        }
        for subgoal in subgoals:
            if len(tasks) >= max_tasks or subgoal["id"] in covered:
                continue
            tasks.append({
                "query": " ".join(
                    part for part in (city, subgoal["objective"]) if part
                ),
                "tool": (
                    "web_search"
                    if "web_search" in available_tools
                    else next(iter(available_tools))
                ),
                "purpose": f"find evidence for: {subgoal['objective']}",
                "subgoal_ids": [subgoal["id"]],
            })
        return tasks[:max_tasks]

    # Model outage fallback preserves the open goal verbatim.  It intentionally
    # does not guess a domain category from keywords.
    fallback_tool = "web_search" if "web_search" in available_tools else next(iter(available_tools))
    fallback_queries = gaps or [
        " ".join(part for part in (city, subgoal["objective"]) if part).strip()
        for subgoal in subgoals
    ] or [" ".join(part for part in (city, goal) if part).strip()]
    return [
        {
            "query": query or city or "周末去处",
            "tool": fallback_tool,
            "purpose": "find source-backed candidates for the user's stated goal",
            "subgoal_ids": (
                [subgoals[index]["id"]]
                if index < len(subgoals)
                else []
            ),
        }
        for index, query in enumerate(fallback_queries[:max_tasks])
    ]


def extract_open_candidates(
    sources: list[dict],
    brief: dict,
    *,
    limit: int = 20,
) -> list[dict]:
    """Extract heterogeneous candidate entities without forcing an Event schema."""
    compact_sources = [
        {
            "source_index": index,
            "title": str(item.get("title") or "")[:300],
            "url": str(item.get("url") or "")[:1000],
            "content": str(item.get("content") or "")[:1600],
        }
        for index, item in enumerate(sources[:30])
        if item.get("url")
    ]
    if not compact_sources:
        return []
    parsed = extract_json(
        "open_candidate_extract",
        """Extract concrete options that a user could actually choose or visit.
The user goal and acceptance criteria are open text.  Candidate kind is also
free text: do not map it to a fixed taxonomy.

Return:
{"candidates":[
 {"title":"entity name","kind":"free-form kind","summary":"source-supported facts",
  "venue":"place/area if known","source_index":0,
  "subgoal_ids":["goal_1"],
  "availability":{"mode":"dated|recurring|always|unknown",
                  "start":null,"end":null,
                  "recurring_hours":["source-stated recurring hours"],
                  "date_specific_status":"confirmed|no_known_conflict|unknown|contradicted"}}
]}
Do not invent an entity, date, address, or suitability claim.  Omit list pages
and generic articles unless a concrete named option can be extracted from them.
The candidate title must name the selectable/visitable entity itself.  A news
story, ranking, discount, or promotion may support facts about an entity but
must not replace that entity unless the user's goal explicitly asks for it.""",
        json.dumps(
            {
                "research_goal": brief.get("research_goal") or brief.get("nl_query"),
                "acceptance_criteria": brief.get("acceptance_criteria") or [],
                "research_subgoals": research_subgoals_from_constraints(brief),
                "city": brief.get("city_name") or brief.get("city_code"),
                "time_window": brief.get("weekend") or {},
                "sources": compact_sources,
            },
            ensure_ascii=False,
        ),
        timeout=get_settings().deep_research_candidate_extract_timeout_s,
    )
    result: list[dict] = []
    seen: set[tuple[str, str]] = set()
    if not isinstance(parsed, dict):
        return result
    for raw in parsed.get("candidates") or []:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        try:
            source_index = int(raw.get("source_index"))
            source = compact_sources[source_index]
        except (TypeError, ValueError, IndexError):
            continue
        if not title or not source.get("url"):
            continue
        key = ("".join(title.lower().split()), source["url"])
        if key in seen:
            continue
        seen.add(key)
        availability = raw.get("availability")
        availability = availability if isinstance(availability, dict) else {}
        mode = str(availability.get("mode") or "unknown")
        if mode not in {"dated", "recurring", "always", "unknown"}:
            mode = "unknown"
        recurring_hours = _texts(availability.get("recurring_hours"))
        date_specific_status = str(
            availability.get("date_specific_status") or ""
        ).strip()
        if date_specific_status not in {
            "confirmed",
            "no_known_conflict",
            "unknown",
            "contradicted",
        }:
            date_specific_status = (
                "no_known_conflict"
                if mode in {"recurring", "always"}
                else "unknown"
            )
        start_at = _optional_iso_datetime(availability.get("start"))
        end_at = _optional_iso_datetime(availability.get("end"))
        if mode == "dated" and start_at and date_specific_status == "unknown":
            date_specific_status = "confirmed"
        valid_subgoal_ids = {
            subgoal["id"]
            for subgoal in research_subgoals_from_constraints(brief)
        }
        subgoal_ids = [
            str(value).strip()
            for value in (raw.get("subgoal_ids") or [])
            if str(value).strip() in valid_subgoal_ids
        ]
        result.append({
            "id": None,
            "title": title,
            "venue": str(raw.get("venue") or "").strip() or None,
            "category": str(raw.get("kind") or "").strip() or "option",
            "candidate_kind": str(raw.get("kind") or "").strip() or "option",
            "candidate_type": "open_candidate",
            "description": str(raw.get("summary") or "").strip(),
            "price_text": None,
            # An open-domain candidate can be a park, route, restaurant, or
            # anything else the user asks for.  Its evidence URL is not
            # necessarily a ticket link, so do not mislabel it as one.
            "booking_url": None,
            "start_at": start_at,
            "end_at": end_at,
            "availability_mode": mode,
            "availability": {
                "mode": mode,
                "start": start_at,
                "end": end_at,
                "recurring_hours": recurring_hours,
                "date_specific_status": date_specific_status,
            },
            "subgoal_ids": list(dict.fromkeys(subgoal_ids)),
            "verification_status": "public_source_observed",
            "rerank_score": 0.0,
            "location": None,
            "evidence": {
                "source_type": "search",
                "source_url": source["url"],
                "verification_status": "public_source_observed",
                "confidence": 0.65,
                "note": "从公开来源抽取；适配性由语义评审单独判断",
            },
            "claims": [
                {
                    "field": "identity",
                    "value": title,
                    "status": "observed",
                    "source_url": source["url"],
                },
                {
                    "field": "availability",
                    "value": {
                        "mode": mode,
                        "recurring_hours": recurring_hours,
                    },
                    "status": date_specific_status,
                    "source_url": source["url"],
                },
            ],
        })
        if len(result) >= limit:
            break
    return result


def evaluate_candidates(
    candidates: list[dict],
    *,
    research_goal: str,
    acceptance_criteria: list[str],
    strict: bool,
    research_subgoals: list[dict] | None = None,
) -> tuple[list[dict], dict]:
    """Judge candidates against the original goal, not against category keywords."""
    goal = str(research_goal or "").strip()
    criteria = _texts(acceptance_criteria)
    explicit_subgoals = bool(research_subgoals)
    subgoals = research_subgoals_from_constraints({
        "research_goal": goal,
        "acceptance_criteria": criteria,
        "research_subgoals": research_subgoals or [],
    })
    required_subgoal_ids = (
        {
            subgoal["id"]
            for subgoal in subgoals
            if subgoal.get("required", True)
        }
        if explicit_subgoals
        else set()
    )
    if not candidates:
        return [], {
            "evaluated": False,
            "matched_count": 0,
            "criterion_coverage": 0.0 if (goal or criteria) else 1.0,
            "gaps": list(criteria),
            "failure": None,
            "covered_subgoal_ids": [],
            "missing_subgoal_ids": sorted(required_subgoal_ids),
            "subgoal_coverage": 0.0 if required_subgoal_ids else 1.0,
        }
    if not goal and not criteria:
        return [dict(item) for item in candidates], {
            "evaluated": False,
            "matched_count": len(candidates),
            "criterion_coverage": 1.0,
            "gaps": [],
            "failure": None,
            "covered_subgoal_ids": sorted(required_subgoal_ids),
            "missing_subgoal_ids": [],
            "subgoal_coverage": 1.0,
        }
    compact = []
    for index, item in enumerate(candidates[:40]):
        compact.append({
            "candidate_index": index,
            "title": item.get("title"),
            "kind": item.get("candidate_kind") or item.get("category"),
            "venue": item.get("venue"),
            "description": item.get("description"),
            "price": item.get("price_text"),
            "availability": {
                "mode": item.get("availability_mode"),
                "start": item.get("start_at"),
                "end": item.get("end_at"),
            },
            "evidence": item.get("evidence"),
            "suggested_subgoal_ids": item.get("subgoal_ids") or [],
        })
    instruction = """Evaluate candidate relevance, not whole-plan completeness.
Use semantic meaning and supplied evidence, not keyword or category matching.
A missing fact is unknown, never assumed true. One candidate only needs to be a
valid component of one relevant subgoal. It must NOT be rejected merely because
other candidates are needed to complete the route or because plan-level facts
about other entities are unknown. Different candidates collectively satisfy a
subgoal and the overall plan. Mentioning a requested entity inside a news story,
promotion, or ranking does not make that wrapper the requested entity.

For each matched subgoal, copy the exact supplied acceptance-criterion strings
that this candidate supports into supported_criteria. Put criteria about other
components into unknown_criteria. `status=matched` means this candidate is a
source-supported selectable component of that subgoal, not that it completes
the entire subgoal by itself.

Return:
{"judgments":[
 {"candidate_index":0,"match":true,"score":0.0,
  "supported_criteria":["..."],"contradicted_criteria":["..."],
  "unknown_criteria":["..."],"reason":"short evidence-based reason",
  "subgoal_assessments":[
   {"subgoal_id":"goal_1","status":"matched|rejected|unknown",
    "supported_criteria":["..."],"contradicted_criteria":[],
    "unknown_criteria":[]}
  ]}
],
"criterion_coverage":0.0,"gaps":["unmet or unverified criterion"]}"""
    settings = get_settings()
    batch_size = max(
        1,
        min(
            40,
            int(
                getattr(
                    settings,
                    "deep_research_semantic_judge_batch_size",
                    10,
                )
            ),
        ),
    )
    batches = [
        compact[offset:offset + batch_size]
        for offset in range(0, len(compact), batch_size)
    ]

    def judge_batch(batch: list[dict]) -> dict | None:
        try:
            return extract_json(
                "candidate_semantic_judge",
                instruction,
                json.dumps(
                    {
                        "research_goal": goal,
                        "acceptance_criteria": criteria,
                        "research_subgoals": subgoals,
                        "candidates": batch,
                    },
                    ensure_ascii=False,
                ),
                # Each source-grounded batch gets the configured upper bound.
                # Batches execute concurrently, so one slow batch does not
                # serialize every other candidate behind it.
                timeout=settings.deep_research_semantic_judge_timeout_s,
            )
        except Exception:
            return None

    batch_results: list[dict | None] = []
    if len(batches) == 1:
        batch_results = [judge_batch(batches[0])]
    else:
        workers = max(
            1,
            min(
                len(batches),
                int(
                    getattr(
                        settings,
                        "deep_research_semantic_judge_concurrency",
                        4,
                    )
                ),
            ),
        )
        indexed_results: dict[int, dict | None] = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(judge_batch, batch): index
                for index, batch in enumerate(batches)
            }
            for future in as_completed(futures):
                indexed_results[futures[future]] = future.result()
        batch_results = [
            indexed_results.get(index) for index in range(len(batches))
        ]

    judgments: dict[int, dict] = {}
    coverage = 0.0
    gaps: list[str] = list(criteria)
    successful_batches = 0
    returned_gaps: list[str] = []
    for parsed in batch_results:
        if not isinstance(parsed, dict):
            continue
        successful_batches += 1
        try:
            batch_coverage = max(
                0.0,
                min(1.0, float(parsed.get("criterion_coverage", 0))),
            )
            coverage = max(coverage, batch_coverage)
        except (TypeError, ValueError):
            pass
        returned_gaps.extend(_texts(parsed.get("gaps")))
        for raw in parsed.get("judgments") or []:
            if not isinstance(raw, dict):
                continue
            try:
                index = int(raw.get("candidate_index"))
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(candidates):
                judgments[index] = raw
    if returned_gaps:
        gaps = list(dict.fromkeys(returned_gaps))

    evaluated = bool(judgments)
    if not evaluated:
        # Infrastructure/model failure is not evidence that a source-backed
        # candidate violates the user's goal.  Preserve only goal-conditioned
        # open candidates in strict mode; legacy Activity rows still fail
        # closed because they may have been recalled for unrelated reasons.
        preserved: list[dict] = []
        for original in candidates:
            if strict and original.get("candidate_type") != "open_candidate":
                continue
            evidence = original.get("evidence") or {}
            if (
                original.get("candidate_type") == "open_candidate"
                and not evidence.get("source_url")
                and evidence.get("source_type") != "amap"
            ):
                continue
            item = dict(original)
            item["semantic_evaluation"] = {
                "status": "unknown",
                "reason": "语义评审暂不可用；保留来源支持的研究候选，尚待核实",
            }
            item["preference_score"] = 0.0
            preserved.append(item)
        return preserved, {
            "evaluated": False,
            "matched_count": 0,
            "criterion_coverage": 0.0,
            "gaps": gaps or list(criteria),
            "failure": "semantic_judge_unavailable",
            "covered_subgoal_ids": [],
            "missing_subgoal_ids": sorted(required_subgoal_ids),
            "subgoal_coverage": 0.0 if required_subgoal_ids else 1.0,
            "batch_count": len(batches),
            "successful_batch_count": successful_batches,
            "failed_batch_count": len(batches) - successful_batches,
            "evaluated_candidate_count": 0,
        }
    partial_evaluation = successful_batches < len(batches)
    ranked: list[tuple[float, int, dict]] = []
    accepted_indices: set[int] = set()
    subgoal_by_id = {subgoal["id"]: subgoal for subgoal in subgoals}
    supported_by_subgoal: dict[str, list[str]] = {
        subgoal_id: [] for subgoal_id in subgoal_by_id
    }
    matched_candidates_by_subgoal: dict[str, set[int]] = {
        subgoal_id: set() for subgoal_id in subgoal_by_id
    }
    globally_supported: list[str] = []
    for index, original in enumerate(candidates):
        item = dict(original)
        raw = judgments.get(index)
        if raw is None:
            item["semantic_evaluation"] = {
                "status": "unknown",
                "reason": (
                    "该候选所在语义评审批次未完成；保留来源证据，尚待核实"
                    if partial_evaluation
                    else "语义评审未返回该候选判断，尚待核实"
                ),
            }
            item["preference_score"] = 0.0
            score = 0.0
            matched = False
        else:
            supported = _texts(raw.get("supported_criteria"))
            contradicted = _texts(raw.get("contradicted_criteria"))
            unknown = _texts(raw.get("unknown_criteria"))
            matched = raw.get("match") is True
            # Candidate relevance and collection completeness are deliberately
            # separate.  Requiring every single candidate to satisfy every
            # route-level criterion is the failure mode that used to erase a
            # perfectly useful multi-stop result set.
            matched_subgoal_ids: list[str] = []
            assessments = raw.get("subgoal_assessments") or []
            if explicit_subgoals and assessments and subgoal_by_id:
                for assessment in assessments:
                    if not isinstance(assessment, dict):
                        continue
                    subgoal_id = str(assessment.get("subgoal_id") or "").strip()
                    subgoal = subgoal_by_id.get(subgoal_id)
                    if not subgoal or assessment.get("status") != "matched":
                        continue
                    subgoal_supported = _texts(assessment.get("supported_criteria"))
                    subgoal_contradicted = _texts(
                        assessment.get("contradicted_criteria")
                    )
                    if subgoal_supported and not subgoal_contradicted:
                        matched_subgoal_ids.append(subgoal_id)
                        supported_by_subgoal[subgoal_id].extend(
                            subgoal_supported
                        )
                        matched_candidates_by_subgoal[subgoal_id].add(index)
                matched = bool(matched_subgoal_ids)
            elif criteria:
                matched = (
                    matched
                    and bool(supported)
                    and not contradicted
                )
                if matched:
                    globally_supported.extend(supported)
            try:
                score = max(0.0, min(1.0, float(raw.get("score", 0))))
            except (TypeError, ValueError):
                score = 0.0
            item["semantic_evaluation"] = {
                "status": "matched" if matched else "rejected",
                "score": score,
                "supported_criteria": supported,
                "contradicted_criteria": contradicted,
                "unknown_criteria": unknown,
                "reason": str(raw.get("reason") or "").strip(),
                "matched_subgoal_ids": matched_subgoal_ids,
            }
            item["subgoal_ids"] = list(dict.fromkeys([
                *(item.get("subgoal_ids") or []),
                *matched_subgoal_ids,
            ]))
            item["preference_score"] = score
            if item["semantic_evaluation"]["supported_criteria"]:
                item["preference_match"] = item["semantic_evaluation"]["supported_criteria"]
                item["preference_match_basis"] = "按原始需求与来源证据进行语义评审"
        preserve_unknown = (
            raw is None
            and item.get("candidate_type") == "open_candidate"
            and bool(
                (item.get("evidence") or {}).get("source_url")
                or (item.get("evidence") or {}).get("source_type") == "amap"
            )
        )
        if not strict or matched or preserve_unknown:
            ranked.append((score, -index, item))
        if matched:
            accepted_indices.add(index)
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    covered_subgoal_ids: set[str] = set()
    collection_gaps: list[str] = []
    for subgoal_id, subgoal in subgoal_by_id.items():
        expected = _texts(subgoal.get("acceptance_criteria"))
        observed = supported_by_subgoal.get(subgoal_id, [])
        supported_all = all(
            any(_same_criterion(criterion, value) for value in observed)
            for criterion in expected
        )
        enough_candidates = (
            len(matched_candidates_by_subgoal.get(subgoal_id, set()))
            >= _target_count(subgoal.get("target_count"))
        )
        if supported_all and enough_candidates:
            covered_subgoal_ids.add(subgoal_id)
        else:
            if not enough_candidates:
                collection_gaps.append(
                    f"{subgoal.get('objective')}: "
                    f"{len(matched_candidates_by_subgoal.get(subgoal_id, set()))}"
                    f"/{_target_count(subgoal.get('target_count'))} candidates"
                )
            collection_gaps.extend(
                criterion
                for criterion in expected
                if not any(
                    _same_criterion(criterion, value)
                    for value in observed
                )
            )
    missing_subgoal_ids = required_subgoal_ids - covered_subgoal_ids
    subgoal_coverage = (
        len(required_subgoal_ids - missing_subgoal_ids)
        / max(1, len(required_subgoal_ids))
    )
    if required_subgoal_ids:
        coverage = subgoal_coverage
        gaps = list(dict.fromkeys(collection_gaps))
    elif criteria:
        supported_count = sum(
            1
            for criterion in criteria
            if any(
                _same_criterion(criterion, value)
                for value in globally_supported
            )
        )
        coverage = supported_count / max(1, len(criteria))
        gaps = [
            criterion
            for criterion in criteria
            if not any(
                _same_criterion(criterion, value)
                for value in globally_supported
            )
        ]
    return [row[2] for row in ranked], {
        "evaluated": evaluated,
        "matched_count": len(accepted_indices),
        "criterion_coverage": coverage,
        "gaps": gaps,
        "failure": "semantic_judge_partial" if partial_evaluation else None,
        "covered_subgoal_ids": sorted(covered_subgoal_ids),
        "missing_subgoal_ids": sorted(missing_subgoal_ids),
        "subgoal_coverage": subgoal_coverage,
        "batch_count": len(batches),
        "successful_batch_count": successful_batches,
        "failed_batch_count": len(batches) - successful_batches,
        "evaluated_candidate_count": len(judgments),
    }
