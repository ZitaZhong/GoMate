"""编排节点实现（DD-02 §5 节点编排契约表）。

- research / dining 调用真实 DD-05 检索（读写解耦：只读 activities）。
- 其余领域节点（DD-06/09/11/12/13）在其详细设计落地前，用确定性桩实现：
  尊重每个节点的读入/写出契约与证据语义（不编造票价/余票，估算一律标 estimated）。
每个节点是 state 的纯函数：给定读入字段，产出写出字段（partial state）。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import text

from ..db import get_session
from ..providers import call as provider_call
from ..domain import build_transport_options
from ..domain import destination_discovery as discover_domain
from ..domain import parse_constraints
from ..domain import plan_dining, plan_hotel_area, plan_local_mobility
from ..domain import solve_timeline, validate_timeline
from ..intel.dedup import same_event_title
from ..retrieval import RetrievalService, Weekend
from ..seeds.activities_dev import upcoming_weekend
from .bundle import compose_confirm_bundle, compose_explore_bundle

try:  # interrupt 仅在图执行期有上下文；导入本身安全
    from langgraph.types import interrupt
except Exception:  # pragma: no cover
    interrupt = None  # type: ignore

_RETRIEVAL = RetrievalService()


class _DeepResearchGate:
    """封装 DD-17 deep_research 触发判定与调用；无 search key 时跳过（不污染库）。"""

    def enabled(self) -> bool:
        from ..research import needs_deep_research
        return needs_deep_research()

    def should_run(self) -> bool:
        from ..providers import has_key
        from ..research import needs_deep_research
        return needs_deep_research() and (has_key("search") or has_key("amap"))

    def run(
        self, code, wk, c, session, on_progress=None, *,
        follow_up_queries=None, feedback=None, exclude_ids=None, plan_id=None,
        time_budget_s=None,
    ) -> dict:
        from ..research import deep_research
        res = deep_research(
            code, wk, c.get("interests"), c.get("query"),
            interests=c.get("interests"),
            follow_up_queries=follow_up_queries,
            feedback=feedback,
            exclude_ids=exclude_ids,
            force_refresh=bool(follow_up_queries or exclude_ids),
            plan_id=plan_id,
            trigger="coverage_gap" if follow_up_queries else "user_explicit",
            on_progress=on_progress, session=session,
            time_budget_s=time_budget_s,
            research_goal=c.get("research_goal"),
            acceptance_criteria=c.get("acceptance_criteria"),
            research_subgoals=c.get("research_subgoals"),
        )
        return {"activity_ids": res.activity_ids, "candidates": res.candidates,
                "job_id": res.job_id,
                "ingested": len(res.activity_ids), "degraded": res.degraded,
                "cache_hit": res.cache_hit, "status": res.status,
                "source_count": res.source_count, "official_count": res.official_count,
                "termination": res.termination, "query_count": res.query_count,
                "round_count": res.round_count, "coverage": res.coverage,
                "marginal_gain": res.marginal_gain, "trace": res.trace,
                "provider_status": res.provider_status,
                "provider_errors": res.provider_errors,
                "elapsed_s": res.elapsed_s}


_deep_research = _DeepResearchGate()


# ============================ evidence helpers ============================
def _ev(source_type: str, vs: str, confidence: float, note: str | None = None, url: str | None = None) -> dict:
    return {
        "source_type": source_type,
        "source_url": url,
        "verification_status": vs,
        "confidence": confidence,
        "note": note,
    }


def _ev_estimated(note: str | None = None) -> dict:
    return _ev("editorial", "estimated", 0.4, note)  # 规则/粗估：明确标 estimated


def _ev_rule(note: str | None = None) -> dict:
    return _ev("editorial", "public_source_observed", 0.6, note)  # 确定性规则/运营内容


def _ev_city(note: str | None = None) -> dict:
    return _ev("open_dataset", "public_source_observed", 0.7, note)  # 城市档案（运营维护）


# ============================ 小工具 ============================
def _weekend(c: dict) -> Weekend:
    ws, we = c.get("weekend_start"), c.get("weekend_end")
    if ws and we:
        return Weekend(datetime.fromisoformat(ws), datetime.fromisoformat(we))
    sat, sun = upcoming_weekend()
    return Weekend(sat, sun)


def _city_center(session, city_code: str) -> tuple[float, float] | None:
    row = session.execute(
        text(
            "SELECT ST_X(center::geometry), ST_Y(center::geometry), name "
            "FROM city_playbook WHERE city_code = :c"
        ),
        {"c": city_code},
    ).first()
    if not row or row[0] is None:
        return None
    return (float(row[0]), float(row[1]))


def _city_name(session, city_code: str) -> str:
    name = session.scalar(text("SELECT name FROM city_playbook WHERE city_code = :c"), {"c": city_code})
    return name or city_code


def _candidate_to_dict(x) -> dict:
    return {
        "id": x.id,
        "title": x.title,
        "venue": x.venue,
        "category": x.category,
        "price_text": x.price_text,
        "booking_url": x.booking_url,
        "start_at": x.start_at.isoformat() if x.start_at else None,
        "end_at": x.end_at.isoformat() if x.end_at else None,
        "verification_status": x.verification_status,
        "rerank_score": x.rerank_score,
        "location": list(x.location) if x.location else None,
        "evidence": x.evidence,
    }


def _stream_writer():
    """LangGraph 自定义流写手（节点内实时进度→SSE）；非流式/不可用时返回 None。"""
    try:
        from langgraph.config import get_stream_writer
        return get_stream_writer()
    except Exception:
        return None


def _in_window(start_iso: str | None, wk: Weekend, end_iso: str | None = None) -> bool:
    """活动区间是否与出行窗口重叠；覆盖“开展已久、周末仍在展”的长展。"""
    if not start_iso:
        return False
    try:
        st = datetime.fromisoformat(start_iso)
        ed = datetime.fromisoformat(end_iso) if end_iso else st
        # 活动源中同时存在 naive/aware 时间。按旅行日历日期比较既能避免
        # TypeError 默认放行旧活动，也符合“当周活动”的日粒度语义。
        if ed.date() < st.date() or (ed.date() - st.date()).days > 370:
            return False
        return st.date() <= wk.end.date() and ed.date() >= wk.start.date()
    except (ValueError, TypeError, AttributeError):
        return False


def _candidate_available(candidate: dict, wk: Weekend) -> bool:
    """Apply time-window semantics without forcing every option into Event."""
    if candidate.get("candidate_type") != "open_candidate":
        return _in_window(candidate.get("start_at"), wk, candidate.get("end_at"))
    availability = candidate.get("availability")
    if (
        isinstance(availability, dict)
        and availability.get("date_specific_status") == "contradicted"
    ):
        return False
    mode = str(candidate.get("availability_mode") or "unknown")
    if mode == "dated":
        return _in_window(candidate.get("start_at"), wk, candidate.get("end_at"))
    return mode in {"always", "recurring", "unknown"}


_CONTEMPORARY_YEAR_RE = re.compile(r"(?<!\d)(20[2-3]\d)(?!\d)")


def _title_year_matches_dates(
    title: str | None,
    start_iso: str | None,
    end_iso: str | None = None,
) -> bool:
    """读路径双保险：拦截历史脏数据里的“标题年份/活动日期”拼接冲突。"""
    claimed = {int(value) for value in _CONTEMPORARY_YEAR_RE.findall(title or "")}
    if not claimed:
        return True
    try:
        actual = {datetime.fromisoformat(start_iso).year} if start_iso else set()
        if end_iso:
            actual.add(datetime.fromisoformat(end_iso).year)
        return bool(claimed & actual)
    except (ValueError, TypeError):
        return False


def _personalize_activities(
    candidates: list[dict],
    soft_preferences: list[str] | None,
    feedback: str | None,
) -> tuple[list[dict], bool]:
    """Semantic reranking over open preference text."""
    from ..research.semantics import evaluate_candidates

    criteria = [
        str(value).strip()
        for value in (soft_preferences or [])
        if str(value).strip()
    ]
    goal = str(feedback or "").strip() or "；".join(criteria)
    ranked, evaluation = evaluate_candidates(
        candidates,
        research_goal=goal,
        acceptance_criteria=criteria,
        strict=False,
    )
    return ranked, bool(evaluation.get("matched_count"))


def _matches_constraint_kinds(activity: dict, interests: list[str] | None) -> bool:
    """Compatibility shim backed by the open semantic judge."""
    from ..research.semantics import evaluate_candidates

    values = [str(value).strip() for value in (interests or []) if str(value).strip()]
    if not values:
        return True
    matched, _evaluation = evaluate_candidates(
        [activity],
        research_goal="；".join(values),
        acceptance_criteria=values,
        strict=True,
    )
    return bool(matched)


def _distinct_activity_entities(
    candidates: list[dict],
    *,
    excluded: list[dict] | None = None,
    feedback: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """按活动实体去重，并排除上一轮同一活动的跨来源标题变体。"""
    comparison = list(excluded or [])
    accepted: list[dict] = []
    def normalized_title(item: dict) -> str:
        text = str(item.get("title") or "")
        # Parenthetical text is commonly an alias or explanatory qualifier
        # (for example "九溪烟树（九溪十八涧）").  Removing it gives a
        # language-agnostic entity key without introducing a place taxonomy.
        text = re.sub(r"[\(（][^()（）]{1,80}[\)）]", "", text)
        return re.sub(r"\s+", "", text).lower()

    comparison_keys = {
        normalized_title(item)
        for item in comparison
        if str(item.get("title") or "").strip()
    }
    accepted_keys: set[str] = set()
    for candidate in candidates:
        key = normalized_title(candidate)
        if not key or key in comparison_keys or key in accepted_keys:
            continue
        if any(same_event_title(candidate.get("title"), old.get("title")) for old in comparison):
            continue
        if any(same_event_title(candidate.get("title"), old.get("title")) for old in accepted):
            continue
        accepted.append(candidate)
        accepted_keys.add(key)
        if limit is not None and len(accepted) >= limit:
            break
    return accepted


def _collection_semantic_evaluation(
    candidates: list[dict],
    research_subgoals: list[dict],
    batch_evaluation: dict,
) -> dict:
    """Aggregate goal coverage over the durable candidate collection.

    Semantic judging is intentionally batched and research can span multiple
    rounds.  A later empty batch must not erase goals already covered by the
    baseline plan or an earlier batch.
    """
    if not research_subgoals:
        return dict(batch_evaluation)
    has_item_level_evaluation = any(
        (item.get("semantic_evaluation") or {}).get("status")
        in {"matched", "rejected"}
        for item in candidates
    )
    if not has_item_level_evaluation:
        # Compatibility for legacy rows and injected retrieval providers that
        # only return the aggregate judge contract.
        return dict(batch_evaluation)
    matched = [
        item
        for item in _distinct_activity_entities(candidates)
        if (item.get("semantic_evaluation") or {}).get("status") == "matched"
    ]
    counts: dict[str, int] = {}
    for item in matched:
        for subgoal_id in (item.get("subgoal_ids") or []):
            key = str(subgoal_id or "").strip()
            if key:
                counts[key] = counts.get(key, 0) + 1
    required = {
        str(item.get("id") or ""): item
        for item in research_subgoals
        if isinstance(item, dict)
        and item.get("required") is not False
        and str(item.get("id") or "").strip()
    }
    covered: set[str] = set()
    gaps: list[str] = []
    for subgoal_id, subgoal in required.items():
        try:
            target_count = int(subgoal.get("target_count") or 1)
        except (TypeError, ValueError):
            target_count = 1
        target_count = max(1, min(20, target_count))
        observed = counts.get(subgoal_id, 0)
        if observed >= target_count:
            covered.add(subgoal_id)
        else:
            gaps.append(
                f"{subgoal.get('objective') or subgoal_id}: "
                f"{observed}/{target_count} candidates"
            )
    missing = set(required) - covered
    coverage = len(covered) / max(1, len(required))
    return {
        **dict(batch_evaluation),
        "evaluated": bool(
            batch_evaluation.get("evaluated") or matched
        ),
        "matched_count": len(matched),
        "criterion_coverage": coverage,
        "gaps": gaps,
        "covered_subgoal_ids": sorted(covered),
        "missing_subgoal_ids": sorted(missing),
        "subgoal_coverage": coverage,
    }


def _baseline_items_to_preserve(
    state: dict,
    baseline: list[dict],
) -> list[dict]:
    """Return the existing plan commitments that an extend turn must preserve.

    The itinerary/plan ledger is the durable expression of what the user has
    already accepted.  Candidate-card ordering is not.  Falling back to four
    items keeps legacy checkpoints useful without allowing an old top-10 page
    to consume every slot needed by a new subgoal.
    """
    requested_titles: list[str] = []
    ledger = dict(state.get("plan_ledger") or {})
    requested_titles.extend(
        str(value).strip()
        for value in (ledger.get("locked_candidate_titles") or [])
        if str(value).strip()
    )
    requested_titles.extend(
        str(item.get("candidate_title") or "").strip()
        for item in (state.get("itinerary_draft") or [])
        if str(item.get("candidate_title") or "").strip()
    )
    requested_titles = list(dict.fromkeys(requested_titles))
    preserved: list[dict] = []
    if requested_titles:
        for title in requested_titles:
            match = next(
                (
                    item
                    for item in baseline
                    if str(item.get("title") or "").strip() == title
                    or same_event_title(item.get("title"), title)
                ),
                None,
            )
            if match is not None:
                preserved.append({**dict(match), "origin": "baseline"})
    if not preserved:
        preserved = [
            {**dict(item), "origin": "baseline"}
            for item in baseline[:4]
        ]
    return _distinct_activity_entities(preserved)


def _select_plan_candidates(
    state: dict,
    *,
    baseline: list[dict],
    fresh_candidates: list[dict],
    revision_mode: str,
    semantic_evaluation: dict,
    limit: int = 10,
) -> tuple[list[dict], list[dict], dict]:
    """Select by plan commitments and subgoal coverage before global Top-K."""
    fresh = _distinct_activity_entities([
        {**dict(item), "origin": "current_research"}
        for item in fresh_candidates
    ])
    if revision_mode != "extend":
        selected = _distinct_activity_entities(fresh, limit=limit)
        selection = {
            "revision_mode": revision_mode,
            "preserved_baseline_count": 0,
            "selected_fresh_count": len(selected),
            "selected_pending_review_count": sum(
                1
                for item in selected
                if (item.get("semantic_evaluation") or {}).get("status")
                == "unknown"
            ),
            "covered_subgoal_ids": list(
                semantic_evaluation.get("covered_subgoal_ids") or []
            ),
            "missing_subgoal_ids": list(
                semantic_evaluation.get("missing_subgoal_ids") or []
            ),
            "selected_titles": [
                str(item.get("title") or "") for item in selected
            ],
        }
        return selected, selected, selection

    preserved = _baseline_items_to_preserve(state, baseline)
    # Always reserve room for fresh evidence.  Explicitly locked commitments
    # are never dropped; the list may exceed the legacy ten-card target when
    # the user has locked more than eight items.
    fresh_capacity = max(2, limit - len(preserved))
    selected_fresh = _distinct_activity_entities(
        fresh,
        excluded=preserved,
        limit=fresh_capacity,
    )
    if selected_fresh:
        selected = _distinct_activity_entities([
            *preserved,
            *selected_fresh,
        ])
    else:
        # A failed extension must leave the previous valid plan intact.
        selected = [
            {**dict(item), "origin": "baseline"}
            for item in baseline
        ]
    selection = {
        "revision_mode": revision_mode,
        "preserved_baseline_count": len(preserved),
        "selected_fresh_count": len(selected_fresh),
        "selected_pending_review_count": sum(
            1
            for item in selected_fresh
            if (item.get("semantic_evaluation") or {}).get("status")
            == "unknown"
        ),
        "covered_subgoal_ids": list(
            semantic_evaluation.get("covered_subgoal_ids") or []
        ),
        "missing_subgoal_ids": list(
            semantic_evaluation.get("missing_subgoal_ids") or []
        ),
        "preserved_titles": [
            str(item.get("title") or "") for item in preserved
        ],
        "selected_fresh_titles": [
            str(item.get("title") or "") for item in selected_fresh
        ],
        "selected_titles": [
            str(item.get("title") or "") for item in selected
        ],
    }
    return selected, selected_fresh, selection


# ============================ 节点 ============================
def constraint_parser(state: dict) -> dict:
    """parse（DD-07）：结构化约束 + 缺省 + 检索 query；缺槽→带 warning 继续。不产事实（不受 Guard）。"""
    c, warnings = parse_constraints(state.get("constraints"))
    return {"constraints": c, "warnings": warnings}


def destination_discovery(state: dict) -> dict:
    """discover（DD-08）：当周活动驱动的候选城市卡（字段带 evidence）。DB 不可用→兜底候选。"""
    c = state["constraints"]
    code = c.get("target_city_code", "310000")
    try:
        with get_session() as s:
            return discover_domain(state, s)
    except Exception as e:  # 降级：DB 不可用仍给出候选
        return {
            "candidate_cities": [{"city_code": code, "name": code, "evidence": _ev_estimated("城市档案不可用")}],
            "errors": [{"node": "discover", "message": str(e)}],
        }


def activity_research(state: dict) -> dict:
    """research（DD-05 库垫场 + DD-17 强制深搜融合）：混合召回+重排得候选活动。

    v2：开启深研且有 search key 时，每轮强制 deep_research（实时入库）+ 复检融合；
    无 key → 跳过深搜（degraded，不污染库），仅返回库垫场。检索空→官方源清单 warning。
    """
    c = state["constraints"]
    budget_started_at = state.get("research_budget_started_at")
    budget_exhausted = bool(state.get("research_budget_exhausted"))
    cities = state.get("candidate_cities") or []
    previous_activities = list(state.get("activities") or [])
    baseline = list(state.get("research_baseline_activities") or previous_activities)
    round_candidates = list(state.get("research_round_candidates") or [])
    active_feedback = state.get("research_active_feedback")
    from ..research.semantics import (
        acceptance_criteria_from_constraints,
        evaluate_candidates,
        research_goal_from_constraints,
        research_subgoals_from_constraints,
    )

    open_requirements = list(c.get("experience_requirements") or [])
    acceptance_criteria = acceptance_criteria_from_constraints(c)
    research_subgoals = research_subgoals_from_constraints(c)
    hard_semantics = bool(
        c.get("research_subgoals")
        or open_requirements
        or c.get("acceptance_criteria")
    )
    fallback_baseline = [] if hard_semantics else baseline
    research_goal = research_goal_from_constraints(c, feedback=active_feedback)
    revision_mode = str(state.get("research_revision_mode") or "initial")
    semantic_evaluation = {
        "evaluated": False,
        "matched_count": 0,
        "criterion_coverage": 1.0 if not acceptance_criteria else 0.0,
        "gaps": list(acceptance_criteria),
        "failure": None,
        "covered_subgoal_ids": [],
        "missing_subgoal_ids": [
            subgoal["id"]
            for subgoal in research_subgoals
            if subgoal.get("required", True)
        ],
        "subgoal_coverage": 0.0 if research_subgoals else 1.0,
    }
    # Do not spend one or two model calls re-judging the previous page before
    # the new research has even started.  A hard goal change also means the old
    # page is not a truthful fallback unless it is evaluated together with the
    # fresh candidates.
    personalized_baseline = baseline
    baseline_personalized = False
    if not cities:
        return {
            "activities": personalized_baseline,
            "research_improved": False,
            "research_personalized": baseline_personalized,
            "research_outcome": "searching",
            "warnings": ["无候选城市，保留上一轮可用方案"],
        }
    code = cities[0]["city_code"]
    wk = _weekend(c)
    writer = _stream_writer()
    # ═══ 研究迭代支持：follow_up_queries 驱动查询演化 + exclude_ids 排除已展示 ═══
    follow_up = state.get("follow_up_queries")
    if follow_up:
        c = {**c, "query": " ".join(follow_up)}
    exclude_ids = set(state.get("shown_activity_ids") or [])
    excluded_entities = baseline + [
        {"title": title} for title in (state.get("shown_activity_titles") or []) if title
    ]
    # 不能先只取 top-10 再排除旧 10 项，否则库里第 11 名以后的候选永远不可见。
    recall_k = min(100, max(20, 10 + 4 * len(exclude_ids)))

    def _emit(phase: str, message: str, found: int = 0) -> None:
        if writer:
            try:
                writer({"phase": phase, "message": message, "found": found})
            except Exception:
                pass

    executed_job_id = None
    research_artifact = None
    try:
        with get_session() as s:
            city_name = _city_name(s, code)
            if budget_exhausted:
                _emit("plan", f"研究预算已用尽，正在复查「{city_name}」已有可信候选…")
            else:
                _emit("plan", f"正在为「{city_name}」研究符合需求的具体去处…")
            cands = _RETRIEVAL.retrieve_activities(s, code, wk, c, top_k=recall_k)
            research_constraints = c
            if revision_mode == "extend" and research_subgoals:
                baseline_covered = {
                    str(subgoal_id)
                    for item in baseline
                    for subgoal_id in (item.get("subgoal_ids") or [])
                    if str(subgoal_id).strip()
                }
                missing_subgoals = [
                    subgoal
                    for subgoal in research_subgoals
                    if subgoal["id"] not in baseline_covered
                ]
                if missing_subgoals:
                    research_constraints = {
                        **c,
                        "research_subgoals": missing_subgoals,
                        "research_goal": "；".join(
                            subgoal["objective"]
                            for subgoal in missing_subgoals
                        ),
                        "acceptance_criteria": [
                            criterion
                            for subgoal in missing_subgoals
                            for criterion in subgoal.get(
                                "acceptance_criteria", []
                            )
                        ],
                    }
            research_meta: dict = {
                **dict(state.get("research") or {}),
                "enabled": _deep_research.enabled(),
            }
            if _deep_research.should_run():
                from ..config import get_settings

                total_budget = float(get_settings().deep_research_time_budget_s)
                if not budget_started_at:
                    budget_started_at = datetime.now(timezone.utc).isoformat()
                try:
                    started = datetime.fromisoformat(str(budget_started_at))
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                except (TypeError, ValueError):
                    elapsed = 0.0
                remaining_budget = max(0.0, total_budget - elapsed)
                if remaining_budget <= 0.05:
                    budget_exhausted = True
                    _emit(
                        "plan",
                        f"研究预算已用尽，改为复查「{city_name}」已有可信候选…",
                    )
                    research_meta.update({
                        "status": "budget_exhausted",
                        "termination": "budget_exhausted",
                    })
                else:
                    _emit("search", "启动实时深度研究（开放域检索+核实入库）…")
                    res = _deep_research.run(
                        code, wk, research_constraints, s,
                        follow_up_queries=follow_up,
                        feedback=active_feedback,
                        exclude_ids=sorted(exclude_ids),
                        plan_id=state.get("plan_id"),
                        time_budget_s=remaining_budget,
                        on_progress=lambda pe: _emit(
                            getattr(pe, "phase", "search"),
                            getattr(pe, "message", ""),
                            getattr(pe, "found", 0),
                        ),
                    )
                    executed_job_id = res.get("job_id")
                    if res.get("activity_ids"):
                        cands = _RETRIEVAL.retrieve_activities(
                            s, code, wk, c, top_k=recall_k
                        )  # 复检融合
                    research_meta.update({
                        key: value for key, value in res.items()
                        if key != "activity_ids"
                    })
                    budget_exhausted = res.get("termination") == "timeout"
                    research_artifact = {
                        "research_id": res.get("job_id"),
                        "goal": research_goal,
                        "subgoals": research_subgoals,
                        "status": res.get("status"),
                        "provider_status": res.get("provider_status") or "ok",
                        "provider_errors": list(
                            res.get("provider_errors") or []
                        )[:5],
                        "source_count": int(res.get("source_count") or 0),
                        "candidate_titles": [
                            str(item.get("title") or "")
                            for item in (res.get("candidates") or [])[:20]
                            if str(item.get("title") or "").strip()
                        ],
                        "trace_summary": (
                            (res.get("trace") or {}).get("summary") or {}
                        ),
                    }
                    if res.get("provider_status") == "unavailable":
                        warning = (
                            "外部搜索服务当前不可用，本轮没有获得新的外部证据；"
                            "这不代表没有符合条件的结果"
                        )
                        return {
                            "activities": baseline,
                            "plan_selected_candidates": baseline,
                            "warnings": [warning],
                            "research": research_meta,
                            "research_artifacts": [research_artifact],
                            "research_round_candidates": [],
                            "research_raw_candidates": [],
                            "research_judged_candidates": [],
                            "research_selection": {
                                "revision_mode": revision_mode,
                                "preserved_baseline_count": len(baseline),
                                "selected_fresh_count": 0,
                                "provider_status": "unavailable",
                            },
                            "research_improved": False,
                            "research_personalized": False,
                            "research_semantic_evaluation": {
                                **semantic_evaluation,
                                "failure": "provider_unavailable",
                                "missing_subgoal_ids": [
                                    subgoal["id"] for subgoal in research_subgoals
                                ],
                                "subgoal_coverage": 0.0,
                            },
                            "research_outcome": "provider_unavailable",
                            "research_budget_started_at": budget_started_at,
                            "research_budget_exhausted": False,
                            "research_should_continue": False,
                        }
            fresh_raw = [
                {**_candidate_to_dict(item), "origin": "current_research"}
                for item in cands
            ]
            fresh_raw.extend(
                dict(candidate)
                for candidate in (research_meta.pop("candidates", []) or [])
            )
            fresh_raw = [
                {**dict(item), "origin": "current_research"}
                for item in fresh_raw
            ]
            before_availability = len(fresh_raw)
            # Event 走日期区间；常设/周期性/未知开放候选保留其自身可用性语义。
            fresh_raw = [
                item for item in fresh_raw
                if str(item.get("title") or "").strip()
                and _candidate_available(item, wk)
                and _title_year_matches_dates(
                    item.get("title"),
                    item.get("start_at"),
                    item.get("end_at"),
                )
            ]
            after_availability = len(fresh_raw)
            # 研究迭代：排除已展示过的活动（对标 Researchify URL 去重）
            if exclude_ids:
                fresh_raw = [
                    item for item in fresh_raw
                    if item.get("id") not in exclude_ids
                ]
            iterative_round = bool(
                follow_up or active_feedback or state.get("research_baseline_activities")
            )
            soft_criteria = [
                str(value).strip()
                for value in (c.get("soft_preferences") or [])
                if str(value).strip()
            ]
            evaluation_criteria = list(dict.fromkeys([
                *acceptance_criteria,
                *soft_criteria,
            ]))
            current_judged = fresh_raw
            if research_goal or evaluation_criteria or research_subgoals:
                current_judged, semantic_evaluation = evaluate_candidates(
                    fresh_raw,
                    research_goal=research_goal,
                    acceptance_criteria=evaluation_criteria,
                    strict=hard_semantics,
                    research_subgoals=research_subgoals,
                )
            previous_raw = list(state.get("research_raw_candidates") or [])
            raw_candidates = _distinct_activity_entities(
                [*previous_raw, *fresh_raw],
                limit=40,
            )
            previous_judged = list(
                state.get("research_judged_candidates") or []
            )
            judged_candidates = _distinct_activity_entities(
                [*previous_judged, *current_judged],
                limit=40,
            )
            semantic_evaluation = _collection_semantic_evaluation(
                [*baseline, *judged_candidates],
                research_subgoals,
                semantic_evaluation,
            )
            fresh_personalized = bool(
                semantic_evaluation.get("matched_count")
                and (soft_criteria or active_feedback)
            )
            eligible_fresh = _distinct_activity_entities(
                judged_candidates,
                excluded=(
                    excluded_entities
                    if iterative_round and revision_mode != "extend"
                    else None
                ),
                feedback=active_feedback if iterative_round else None,
            )
            selected, selected_fresh, selection = _select_plan_candidates(
                state,
                baseline=baseline,
                fresh_candidates=eligible_fresh,
                revision_mode=revision_mode,
                semantic_evaluation=semantic_evaluation,
            )
            acts = selected if selected else fallback_baseline
            personalized = (
                fresh_personalized if selected_fresh else baseline_personalized
            )
            matched_fresh_count = sum(
                1
                for item in selected_fresh
                if (
                    not hard_semantics
                    or (item.get("semantic_evaluation") or {}).get("status")
                    == "matched"
                    or (
                        not item.get("semantic_evaluation")
                        and semantic_evaluation.get("matched_count")
                    )
                )
            )
            pending_fresh_count = int(
                selection.get("selected_pending_review_count") or 0
            )
            improved = matched_fresh_count > 0
            semantic_failure = semantic_evaluation.get("failure")
            partial_unverified = bool(
                selected_fresh
                and (
                    semantic_failure in {
                        "semantic_judge_unavailable",
                        "semantic_judge_partial",
                    }
                    or pending_fresh_count
                )
            )
            selection.update({
                "raw_candidate_count": len(raw_candidates),
                "judged_candidate_count": len(judged_candidates),
                "matched_fresh_count": matched_fresh_count,
                "candidate_loss_anomaly": bool(
                    raw_candidates and not selected_fresh
                ),
            })
            selection_trace = {
                "retrieved_count": before_availability,
                "after_availability_count": after_availability,
                "raw_candidate_count": len(raw_candidates),
                "judged_candidate_count": len(judged_candidates),
                "selected_fresh_count": len(selected_fresh),
                "preserved_baseline_count": int(
                    selection.get("preserved_baseline_count") or 0
                ),
                "selected_count": len(acts),
                "semantic_evaluation": semantic_evaluation,
                "selection": selection,
                "selected": [
                    {
                        "id": item.get("id"),
                        "title": item.get("title"),
                        "candidate_type": item.get("candidate_type"),
                        "semantic_evaluation": item.get("semantic_evaluation"),
                    }
                    for item in acts
                ],
            }
            trace = dict(research_meta.get("trace") or {})
            trace["selections"] = [
                *list(trace.get("selections") or []),
                selection_trace,
            ][-5:]
            research_meta["trace"] = trace
            if research_artifact is not None:
                research_artifact.update({
                    "raw_candidate_titles": [
                        str(item.get("title") or "")
                        for item in raw_candidates
                        if str(item.get("title") or "").strip()
                    ],
                    "selected_candidate_titles": [
                        str(item.get("title") or "")
                        for item in acts
                        if str(item.get("title") or "").strip()
                    ],
                    "selection": selection,
                    "covered_subgoal_ids": list(
                        semantic_evaluation.get("covered_subgoal_ids") or []
                    ),
                    "missing_subgoal_ids": list(
                        semantic_evaluation.get("missing_subgoal_ids") or []
                    ),
                    "summary": (
                        f"本轮抽取 {len(raw_candidates)} 个候选，"
                        f"新增采用 {len(selected_fresh)} 个，"
                        f"保留既有 {selection.get('preserved_baseline_count', 0)} 个；"
                        f"覆盖 {len(semantic_evaluation.get('covered_subgoal_ids') or [])}"
                        f"/{len(research_subgoals)} 个必选目标"
                    ),
                })
            if executed_job_id:
                from ..models import DeepResearchJob

                job = s.get(DeepResearchJob, executed_job_id)
                if job is not None:
                    job.query = {
                        **dict(job.query or {}),
                        "trace": trace,
                    }
            progress_message = (
                f"本轮新增 {len(selected_fresh)} 个候选，"
                f"保留 {selection.get('preserved_baseline_count', 0)} 个既有安排"
            )
            if pending_fresh_count:
                progress_message += f"；{pending_fresh_count} 个待语义复核"
            _emit("done", progress_message, len(selected_fresh))
            if not selected_fresh and not fallback_baseline:  # 首轮库空 → 输出官方源清单
                rows = s.execute(text(
                    "SELECT name, entry_url FROM source_registry "
                    "WHERE (city_code = :c OR city_code IS NULL) AND source_type IN "
                    "('official_venue','culture_bureau','open_dataset') AND enabled = TRUE "
                    "LIMIT 10"
                ), {"c": code}).all()
                research_meta["official_sources"] = [{"name": r[0], "url": r[1]} for r in rows if r[1]]
    except Exception as e:  # 降级：检索失败不整链失败
        return {
            "activities": fallback_baseline,
            "plan_selected_candidates": fallback_baseline,
            "research_round_candidates": round_candidates,
            "research_raw_candidates": list(
                state.get("research_raw_candidates") or []
            ),
            "research_judged_candidates": list(
                state.get("research_judged_candidates") or []
            ),
            "research_selection": dict(
                state.get("research_selection") or {}
            ),
            "research_improved": False,
            "research_personalized": baseline_personalized,
            "research_outcome": "searching",
            "research_budget_started_at": budget_started_at,
            "research_budget_exhausted": budget_exhausted,
            "errors": [{"node": "research", "message": str(e)}],
            "warnings": ["活动检索失败，保留上一轮可用方案并继续换角度"],
        }
    if improved or partial_unverified:
        warnings = []
    elif iterative_round and not budget_exhausted:
        # This is an intermediate research angle.  Reflect may immediately
        # launch another one, so do not leak a transient "not found" warning
        # into the final UI's append-only warning stream.
        warnings = []
    elif baseline and baseline_personalized:
        warnings = [
            f"本轮暂未找到新的可信活动；已按新增偏好重新排序上一轮 {len(baseline)} 项并标注匹配信号"
        ]
    elif baseline:
        warnings = (
            ["本轮未找到满足新要求且证据充分的候选；不会用旧方案冒充匹配结果"]
            if hard_semantics
            else [f"本轮暂未找到更贴近反馈的新活动，保留上一轮 {len(baseline)} 项并继续换角度搜索"]
        )
    else:
        n = len(research_meta.get("official_sources") or [])
        warnings = [f"活动检索为空：可粘贴官方链接继续；已附 {n} 个官方源" if n else "活动检索为空：可粘贴官方活动链接"]
    return {
        "activities": acts,
        "plan_selected_candidates": acts,
        "warnings": warnings,
        "research": research_meta,
        "research_artifacts": (
            [research_artifact] if research_artifact is not None else []
        ),
        "research_round_candidates": selected_fresh,
        "research_raw_candidates": raw_candidates,
        "research_judged_candidates": judged_candidates,
        "research_selection": selection,
        "research_improved": improved,
        "research_personalized": bool(iterative_round and personalized),
        "research_semantic_evaluation": semantic_evaluation,
        "research_outcome": (
            "partial_unverified" if partial_unverified
            else "improved" if iterative_round and improved
            else "searching" if iterative_round
            else "initial"
        ),
        "research_budget_started_at": budget_started_at,
        "research_budget_exhausted": budget_exhausted,
    }


# ============================ 研究迭代（对标 Researchify reflect-loop）============================

_REFLECT_MAX_LOOPS = 3  # 研究回环上限：超 3 次强制进入下游（避免死循环）


def _assess_research_quality(state: dict, activities: list[dict]) -> dict:
    """基于实体、证据、来源和查询覆盖评估充分性，并显式列出缺口。"""
    from ..research.schemas import ResearchQuality

    research = dict(state.get("research") or {})
    titles = {
        re.sub(r"\s+", "", str(item.get("title") or "")).lower()
        for item in activities
        if str(item.get("title") or "").strip()
    }
    evidence_count = sum(1 for item in activities if item.get("evidence"))
    constraints = dict(state.get("constraints") or {})
    semantic = dict(state.get("research_semantic_evaluation") or {})
    has_open_criteria = bool(
        constraints.get("experience_requirements")
        or constraints.get("acceptance_criteria")
        or constraints.get("research_subgoals")
    )
    semantic_evaluated = bool(semantic.get("evaluated"))
    semantic_match_count = int(semantic.get("matched_count") or 0)
    required_subgoal_ids = {
        str(item.get("id") or "")
        for item in (constraints.get("research_subgoals") or [])
        if isinstance(item, dict)
        and item.get("required") is not False
        and str(item.get("id") or "").strip()
    }
    required_candidate_count = 0
    for item in (constraints.get("research_subgoals") or []):
        if (
            not isinstance(item, dict)
            or item.get("required") is False
            or not str(item.get("id") or "").strip()
        ):
            continue
        try:
            target_count = int(item.get("target_count") or 1)
        except (TypeError, ValueError):
            target_count = 1
        required_candidate_count += max(1, min(20, target_count))
    criterion_coverage = float(
        semantic.get("criterion_coverage")
        if semantic.get("criterion_coverage") is not None
        else (0.0 if has_open_criteria else 1.0)
    )
    semantic_ok = (
        not has_open_criteria
        or (
            semantic_evaluated
            and semantic_match_count > 0
            and criterion_coverage >= (
                1.0 if required_subgoal_ids else 0.6
            )
        )
    )
    live_attempted = bool(research.get("status"))
    enough_entities = len(titles) >= (
        max(1, required_candidate_count) if required_subgoal_ids else 3
    )
    evidence_ok = not live_attempted or evidence_count >= min(3, len(titles))
    sources_ok = (
        not live_attempted
        or bool(research.get("cache_hit"))
        or int(research.get("source_count") or 0) > 0
    )
    gaps = []
    if not enough_entities:
        gaps.append("distinct_entities")
    if not evidence_ok:
        gaps.append("evidence")
    if not sources_ok:
        gaps.append("sources")
    if live_attempted and float(research.get("coverage") or 0.0) < 0.5:
        gaps.append("query_coverage")
    if not semantic_ok:
        gaps.extend(semantic.get("gaps") or ["semantic_acceptance"])
    quality = ResearchQuality(
        activity_count=len(activities),
        distinct_entity_count=len(titles),
        evidence_count=evidence_count,
        source_count=int(research.get("source_count") or 0),
        official_count=int(research.get("official_count") or 0),
        query_count=int(research.get("query_count") or 0),
        round_count=int(research.get("round_count") or 0),
        coverage=float(research.get("coverage") or 0.0),
        criterion_coverage=criterion_coverage,
        semantic_match_count=semantic_match_count,
        semantic_evaluated=semantic_evaluated,
        marginal_gain=float(research.get("marginal_gain") or 0.0),
        termination=str(research.get("termination") or "not_run"),
        sufficient=enough_entities and evidence_ok and sources_ok and semantic_ok,
        gaps=list(dict.fromkeys(gaps)),
    )
    return quality.model_dump(mode="json")


def _generate_follow_up_queries(
    interests: list[str] | None,
    feedback: str | None,
    exclude_titles: list[str],
    city_name: str,
    previous_queries: list[str] | None = None,
    *,
    research_goal: str | None = None,
    acceptance_criteria: list[str] | None = None,
) -> list[str]:
    """基于用户反馈和已展示内容生成 gap-driven follow-up 查询。

    对标 Researchify generate_queries 后续轮：利用反思历史聚焦 gap。
    LLM 优先；无 LLM 则规则降级。
    """
    from ..providers import extract_json

    exclude_hint = "、".join(exclude_titles[:5]) if exclude_titles else ""
    goal = (research_goal or "").strip() or "；".join(interests or [])
    criteria = [
        str(value).strip()
        for value in (acceptance_criteria or [])
        if str(value).strip()
    ]

    previous = [q.strip() for q in (previous_queries or []) if q and q.strip()]
    parsed = extract_json(
        "follow_up_queries",
        f"""用户对当前推荐的活动不满意。根据用户反馈生成 2-3 个新的搜索查询，
要求：
1. 搜索方向应不同于已展示的活动
2. 关注用户反馈中暗示的偏好方向
3. 每个查询要具体、有差异化

已展示过的活动（需排除/避开类似的）：{exclude_hint}
之前已经使用的查询（不得原样重复）：{"；".join(previous[:6]) or "无"}
原始研究目标：{goal or "寻找适合的周末去处"}
尚需满足的验收标准：{"；".join(criteria) or "按原始目标判断"}
城市：{city_name}

输出 JSON {{"queries": ["查询1", "查询2", ...]}}""",
        feedback or "不满意当前结果，想看更多不同的",
    )
    if isinstance(parsed, dict) and parsed.get("queries"):
        fresh = [
            str(q).strip() for q in parsed["queries"]
            if str(q).strip() and str(q).strip() not in set(previous)
        ]
        if fresh:
            return list(dict.fromkeys(fresh))[:3]
    # 规则降级：基于兴趣+排除生成
    base = f"{city_name} {goal or '周末去处'}"
    feedback_hint = (feedback or "").strip()[:20]
    candidates = [
        f"{base} {feedback_hint} 本周末 新活动 官方".strip(),
        f"{base} 小众 特色 周末限定",
        f"{base} 新上架 未推荐过",
        f"{base} 近期 热门 官方日程",
    ]
    fresh = [q for q in candidates if q not in set(previous)]
    return list(dict.fromkeys(fresh or candidates[-2:]))[:3]


def activity_reflection(state: dict) -> dict:
    """对标 Researchify reflect：评估活动调研是否充分；用户反馈触发 gap 分析。

    - 无反馈 + 有足够活动 → 充分（is_sufficient=True），记录 shown_ids 后继续下游
    - 有反馈 → 生成 follow_up_queries，记录反思历史，清空反馈
    """
    activities = state.get("activities") or []
    feedback = state.get("research_feedback")
    loop_count = state.get("research_loop_count", 0)
    shown_ids = state.get("shown_activity_ids") or []
    shown_titles = state.get("shown_activity_titles") or []
    constraints = state.get("constraints") or {}
    cities = state.get("candidate_cities") or []
    city_name = cities[0].get("name", "") if cities else ""
    quality = _assess_research_quality(state, activities)

    semantic = dict(state.get("research_semantic_evaluation") or {})
    research = dict(state.get("research") or {})
    if (
        research.get("provider_status") == "unavailable"
        or semantic.get("failure") == "provider_unavailable"
    ):
        return {
            "activities": activities,
            "shown_activity_ids": shown_ids,
            "shown_activity_titles": shown_titles,
            "research_should_continue": False,
            "research_feedback": None,
            "research_active_feedback": None,
            "research_baseline_activities": [],
            "research_round_candidates": [],
            "research_outcome": "provider_unavailable",
            "research_quality": quality,
            "research_stop_reason": "provider_unavailable",
        }
    if (
        semantic.get("failure") in {
            "semantic_judge_unavailable",
            "semantic_judge_partial",
        }
        and (state.get("research_raw_candidates") or activities)
    ):
        current_ids = [a["id"] for a in activities if a.get("id")]
        current_titles = [
            str(a.get("title") or "")
            for a in activities
            if str(a.get("title") or "").strip()
        ]
        return {
            "activities": activities,
            "shown_activity_ids": list(set(
                (state.get("shown_activity_ids") or []) + current_ids
            )),
            "shown_activity_titles": list(dict.fromkeys(
                (state.get("shown_activity_titles") or []) + current_titles
            )),
            "research_should_continue": False,
            "research_feedback": None,
            "research_active_feedback": None,
            "research_baseline_activities": [],
            # Raw/judged/round candidates remain available to the response
            # composer and replay tooling.  A judge outage must not erase
            # successful search and extraction work.
            "research_round_candidates": list(
                state.get("research_round_candidates") or []
            ),
            "research_raw_candidates": list(
                state.get("research_raw_candidates") or []
            ),
            "research_judged_candidates": list(
                state.get("research_judged_candidates") or []
            ),
            "research_selection": dict(
                state.get("research_selection") or {}
            ),
            "plan_selected_candidates": activities,
            "research_improved": False,
            "research_outcome": "partial_unverified",
            "research_quality": quality,
            "research_stop_reason": semantic.get("failure"),
            "warnings": [
                "已保留本轮来源支持的新候选；语义复核未全部完成，"
                "未确认项已标为待核实，不会冒充已验证匹配"
            ],
        }

    if state.get("research_budget_exhausted"):
        return {
            "activities": activities,
            "research_quality": quality,
            "research_stop_reason": "budget_exhausted",
            "research_should_continue": False,
            "research_feedback": None,
            "research_active_feedback": None,
        }

    # 当前轮新产出的活动 IDs
    current_ids = [a["id"] for a in activities if a.get("id")]
    all_shown = list(set(shown_ids + current_ids))
    current_titles = [a.get("title", "") for a in activities if a.get("title")]
    all_shown_titles = list(dict.fromkeys(shown_titles + current_titles))

    active_feedback = state.get("research_active_feedback")
    baseline = list(state.get("research_baseline_activities") or [])
    round_candidates = list(state.get("research_round_candidates") or [])
    improved = state.get("research_improved")
    personalized = bool(state.get("research_personalized"))
    used_queries = list(state.get("follow_up_queries") or [])
    for entry in state.get("research_history") or []:
        used_queries.extend(entry.get("follow_up_queries") or [])
    used_queries = list(dict.fromkeys(used_queries))

    # 用户反馈后的某一检索角度没有新候选：不能拿旧 5 项冒充“充分”，必须换角度继续。
    if not feedback and active_feedback and not improved:
        # 单轮深研内部已经并行覆盖多个子主题；若严格过滤后没有新实体，
        # 但 baseline 已按新增软偏好产生可解释重排，就诚实返回重排结果。
        # 继续跑两轮外部研究只会把用户等待从约 5 分钟放大到约 15 分钟。
        if personalized:
            return {
                "activities": activities,
                "shown_activity_ids": all_shown,
                "shown_activity_titles": all_shown_titles,
                "research_active_feedback": None,
                "research_baseline_activities": [],
                "research_round_candidates": [],
                "research_outcome": "reranked",
                "research_should_continue": False,
                "research_quality": quality,
                "research_stop_reason": "personalized_rerank",
            }
        if loop_count >= _REFLECT_MAX_LOOPS:
            return {
                "activities": activities if personalized else baseline or activities,
                "shown_activity_ids": all_shown,
                "shown_activity_titles": all_shown_titles,
                "research_active_feedback": None,
                "research_baseline_activities": [],
                "research_round_candidates": [],
                "research_outcome": (
                    "reranked" if personalized else "no_better_alternatives"
                ),
                "research_should_continue": False,
                "research_quality": quality,
                "research_stop_reason": "max_loops",
            }
        retry_queries = _generate_follow_up_queries(
            interests=constraints.get("interests"),
            feedback=active_feedback,
            exclude_titles=[a.get("title", "") for a in baseline],
            city_name=city_name,
            previous_queries=used_queries,
            research_goal=constraints.get("research_goal"),
            acceptance_criteria=constraints.get("acceptance_criteria"),
        )
        retry_count = loop_count + 1
        return {
            "research_history": [{
                "loop": retry_count,
                "feedback": active_feedback,
                "reason": "no_improvement_retry",
                "shown_count": len(shown_ids),
                "follow_up_queries": retry_queries,
            }],
            "research_loop_count": retry_count,
            "follow_up_queries": retry_queries,
            "research_feedback": None,
            "research_should_continue": bool(retry_queries),
            "research_quality": quality,
            "research_stop_reason": "continue_for_gap",
        }

    # 无反馈 + 有足够的新结果 → 充分，直接继续
    if not feedback and quality["sufficient"]:
        return {
            "research_loop_count": loop_count,
            "shown_activity_ids": list(set(shown_ids + current_ids)),
            "shown_activity_titles": all_shown_titles,
            "research_should_continue": False,
            "research_active_feedback": None,
            "research_baseline_activities": [],
            "research_round_candidates": [],
            "research_outcome": (
                "improved" if active_feedback and improved
                else state.get("research_outcome") or "initial"
            ),
            "research_quality": quality,
            "research_stop_reason": "quality_sufficient",
        }

    # 反馈或覆盖缺口 → 生成 follow-up queries（排除已展示和已用查询）
    if loop_count >= _REFLECT_MAX_LOOPS:
        revision_mode = str(
            state.get("research_revision_mode") or "initial"
        )
        return {
            "activities": (
                activities
                if revision_mode == "extend"
                else activities
                if personalized and not round_candidates
                else round_candidates or baseline or activities
            ),
            "shown_activity_ids": all_shown,
            "shown_activity_titles": all_shown_titles,
            "research_feedback": None,
            "research_active_feedback": None,
            "research_baseline_activities": [],
            "research_round_candidates": [],
            "research_outcome": (
                ("reranked" if personalized else "no_better_alternatives")
                if active_feedback
                else state.get("research_outcome")
            ),
            "research_should_continue": False,
            "research_quality": quality,
            "research_stop_reason": "max_loops",
        }
    follow_up = _generate_follow_up_queries(
        interests=constraints.get("interests"),
        feedback=feedback or active_feedback,
        exclude_titles=all_shown_titles,
        city_name=city_name,
        previous_queries=used_queries,
        research_goal=constraints.get("research_goal"),
        acceptance_criteria=constraints.get("acceptance_criteria"),
    )

    # 记录本轮反思（累积式）
    reflection_entry = {
        "loop": loop_count + 1,
        "feedback": feedback,
        "shown_count": len(all_shown),
        "follow_up_queries": follow_up,
    }
    return {
        "research_history": [reflection_entry],  # operator.add 追加
        "research_loop_count": loop_count + 1,
        "follow_up_queries": follow_up,
        "shown_activity_ids": all_shown,
        "shown_activity_titles": all_shown_titles,
        "research_feedback": None,  # 清空已处理的反馈
        "research_active_feedback": feedback or active_feedback,
        "research_baseline_activities": (
            list(activities) if feedback else baseline
        ),
        "research_round_candidates": [] if feedback else round_candidates,
        "research_should_continue": bool(follow_up),
        "research_quality": quality,
        "research_stop_reason": "continue_for_gap",
    }


def transport_strategy(state: dict) -> dict:
    """transport（DD-09）：确定性门到门策略卡 + 起售 + 12306 深链 + 预填。禁编票价/余票。"""
    c = state["constraints"]
    cities = state.get("candidate_cities") or []
    opts = build_transport_options(c, cities, state.get("origin"))
    return {"transport_options": opts}


def await_booking_node(state: dict) -> dict:
    """await_booking：产出探索版 → interrupt 持久化 → 等用户回填（跨天恢复）。"""
    from ..copilot.respond import compose_research_response

    # 天气信号在图中位于本节点之后；研究 Run 在此 interrupt 返回，若不就地补算，
    # 合成回复就拿不到 indoor_pref/replan_reason——会在暴雨语境静默新增户外项。
    weather = state.get("weather")
    if not weather:
        try:
            weather = weather_awareness(state).get("weather")
        except Exception:
            weather = None
    scoped = {**state, "weather": weather} if weather else state
    response = compose_research_response(scoped)
    explore_bundle = compose_explore_bundle({**scoped, **response})
    to = state.get("transport_options") or {}
    user_bookings = interrupt(
        {
            "type": "await_booking",
            "explore_bundle": explore_bundle,
            "prefill": to.get("prefill", {}),
            "presale_reminders": to.get("presale", []),
        }
    )
    return {"bookings": user_bookings or [], "stage": "confirm"}


def hotel_area_planning(state: dict) -> dict:
    """hotel（DD-11）：回填酒店优先，否则城市档案住宿区。"""
    try:
        with get_session() as s:
            return {"hotel_area": plan_hotel_area(state, s)}
    except Exception as e:  # 降级
        return {"hotel_area": {"name": "市中心", "evidence": _ev_estimated("住宿区降级")},
                "errors": [{"node": "hotel", "message": str(e)}]}


def local_mobility(state: dict) -> dict:
    """mobility（DD-11）：逐段门到门接驳（规则估算，标 estimated）。"""
    legs = plan_local_mobility(state.get("activities") or [], state.get("hotel_area") or {})
    return {"local_routes": legs}


def dining_planning(state: dict) -> dict:
    """dining（DD-11 + DD-05 重排）：动线契合+偏好+忌讳；永远留一个稳妥备选。"""
    try:
        with get_session() as s:
            dining = plan_dining(state, state.get("hotel_area") or {}, _RETRIEVAL, s)
    except Exception as e:  # 降级
        return {"dining": [], "errors": [{"node": "dining", "message": str(e)}],
                "warnings": ["餐饮检索失败，已降级"]}
    return {"dining": dining} if dining else {
        "dining": [],
        "warnings": ["餐饮：暂无可信来源数据（可粘贴点评/小红书链接），未编造"],
    }


def weather_awareness(state: dict) -> dict:
    """weather（DD-12 天气重规划）：消费 replan_reason / qweather 预警 → 恶劣天气偏好室内 + 可见提醒。

    无 qweather key 且无用户声明恶劣天气 → 不臆测（honest，不改变行为）。
    """
    reason = state.get("replan_reason") or ""
    adverse = any(k in reason for k in ("weather", "暴雨", "大雨", "雷雨", "台风", "下雨", "下雪", "雨雪"))
    source, detail = ("user_declared", reason) if adverse else (None, None)
    if not adverse:  # 有 qweather key 时查预警（无 key→unknown，不臆测）
        cities = state.get("candidate_cities") or []
        center = cities[0].get("center") if cities else None
        if isinstance(center, (list, tuple)) and len(center) == 2:
            res = provider_call("qweather", "warning", {"location": f"{center[0]},{center[1]}"})
            if res.ok and res.data and res.data.get("warning"):
                adverse, source, detail = True, "qweather", "气象预警"
    if not adverse:
        return {"weather": {"adverse": False, "evidence": _ev_estimated("无恶劣天气信号")}}
    ev = _ev("qweather" if source == "qweather" else "user_provided",
             "public_source_observed" if source == "qweather" else "estimated",
             0.6, f"恶劣天气：{detail}")
    return {
        "weather": {"adverse": True, "indoor_pref": True, "source": source, "detail": detail, "evidence": ev},
        "warnings": [f"天气提示：{detail or '恶劣天气'} — 已优先安排室内活动，户外项请关注临场天气"],
    }


def timeline_solver(state: dict) -> dict:
    """timeline（DD-12）：贪心排点（锚点→活动→餐→返程缓冲）；恶劣天气偏好室内。证据透传不新造。"""
    slots = solve_timeline(state.get("activities") or [], state.get("dining") or [],
                           state.get("bookings") or [], state.get("constraints") or {},
                           weather=state.get("weather"))
    return {"timeline": slots}


def feasibility_validator(state: dict) -> dict:
    """validate（DD-12）：硬约束校验；累计 attempts 供 route_after_validate 熔断。"""
    prev_attempts = (state.get("validation") or {}).get("metrics", {}).get("attempts", 0)
    validation = validate_timeline(state.get("timeline") or [], state.get("constraints") or {},
                                   state.get("bookings") or [], attempts=prev_attempts + 1)
    issues = validation.get("issues") or []
    warnings = [] if not issues else [f"校验发现硬冲突：{issues}（将重排/熔断）"]
    return {"validation": validation, "warnings": warnings}


def plan_composer(state: dict) -> dict:
    """compose（DD-13）：确认版 bundle + 提醒落库；出稿前跑最终闸（Guard KPI① + 交通禁编 KPI③）。"""
    from ..copilot.respond import compose_confirm_reply
    from ..domain.compose import build_reminders, run_final_gate

    # 确认版自洽回复：基于已排定 timeline（含精确时间）生成，写入 state 供 bundle 携带；
    # 使 replan_weather 等路径产出自然语言回复，不再降级为"见卡片"。
    confirm_reply = compose_confirm_reply(state)
    scoped = {**state, "assistant_response": confirm_reply} if confirm_reply else state
    bundle = compose_confirm_bundle(scoped)
    run_final_gate(bundle)  # 任一硬 KPI 违规 → 抛出拦截（DD-02 §12 / DD-13 §5.3）
    reminders = build_reminders(bundle)
    bundle["reminders"] = reminders  # §06 确认版：随身带提醒摘要
    try:  # P1-8：提醒落库（reminders 表），到期由 dispatch_due_reminders 投递；失败不阻断出稿
        from ..notify import persist_reminders
        with get_session() as s:
            persist_reminders(state.get("plan_id"), reminders, session=s)
            s.commit()
    except Exception:
        pass
    return {"bundle": bundle, "stage": "confirm"}
