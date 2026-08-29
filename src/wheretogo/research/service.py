"""DD-17 实时深度研究服务：每次需求强制全量实时深搜（有界迭代 + 流式进度 + 实时入库）。

不做"库够就跳过"门控（开启时强制执行）；靠流式进度 + 超时 partial + 防抖缓存控延迟/成本。
护栏（DD-03）不动：每条带 evidence；未核实=observed/unknown；交通字段禁编。
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db.session import SessionLocal
from ..models import DeepResearchCache, DeepResearchJob
from .brief import build_brief
from .semantics import (
    acceptance_criteria_from_constraints,
    research_goal_from_constraints,
)
from .supervisor import run_research_loop


@dataclass
class ProgressEvent:
    phase: str  # plan|search|verify|extract|ingest|recheck
    message: str
    found: int = 0
    official: int = 0


@dataclass
class DeepResearchResult:
    activity_ids: list[int] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    official_count: int = 0
    source_count: int = 0
    degraded: bool = False
    cache_hit: bool = False
    job_id: int | None = None
    status: str = "no_results"
    termination: str = "completed"
    query_count: int = 0
    round_count: int = 0
    coverage: float = 0.0
    marginal_gain: float = 0.0
    trace: dict = field(default_factory=dict)
    provider_status: str = "ok"
    provider_errors: list[dict] = field(default_factory=list)
    elapsed_s: float = 0.0


def needs_deep_research(intent: str | None = None, settings: Settings | None = None) -> bool:
    """总开关 + 强制全量（不做"库够跳过"）。仅 ask_info/chitchat 不触发。"""
    s = settings or get_settings()
    if not s.deep_research_enabled:
        return False
    return intent in (None, "provide_constraints", "refine_field", "deep_research", "clarify_answer")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(weekend, key: str) -> str | None:
    v = getattr(weekend, key, None) if weekend else None
    return v.isoformat() if hasattr(v, "isoformat") else v


_CACHE_SCHEMA_VERSION = 4


def _query_hash(
    city_code: str,
    weekend,
    categories: list[str] | None,
    nl_query: str | None = None,
    *,
    interests: list[str] | None = None,
    follow_up_queries: list[str] | None = None,
    feedback: str | None = None,
    exclude_ids: list[int] | None = None,
    research_goal: str | None = None,
    acceptance_criteria: list[str] | None = None,
    research_subgoals: list[dict] | None = None,
    scope: str = "cross_city",
) -> str:
    """对完整研究意图做稳定哈希，防止多轮反馈错误命中首轮缓存。"""
    payload = {
        "v": _CACHE_SCHEMA_VERSION,
        "city": city_code,
        "weekend": [_iso(weekend, "start"), _iso(weekend, "end")],
        "categories": sorted(set(categories or [])),
        "interests": sorted(set(interests or [])),
        "nl_query": (nl_query or "").strip(),
        "follow_up_queries": [q.strip() for q in (follow_up_queries or []) if q.strip()],
        "feedback": (feedback or "").strip(),
        "research_goal": (research_goal or "").strip(),
        "acceptance_criteria": [
            str(value).strip()
            for value in (acceptance_criteria or [])
            if str(value).strip()
        ],
        "research_subgoals": list(research_subgoals or []),
        "exclude_ids": sorted(set(exclude_ids or [])),
        "scope": scope,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def deep_research(
    city_code: str,
    weekend,
    categories: list[str] | None = None,
    nl_query: str | None = None,
    interests: list[str] | None = None,
    follow_up_queries: list[str] | None = None,
    feedback: str | None = None,
    exclude_ids: list[int] | None = None,
    force_refresh: bool = False,
    plan_id: str | int | None = None,
    trigger: str = "user_explicit",
    on_progress: Callable[[ProgressEvent], None] | None = None,
    session: Session | None = None,
    allow_fetch=None,
    time_budget_s: float | None = None,
    research_goal: str | None = None,
    acceptance_criteria: list[str] | None = None,
    research_subgoals: list[dict] | None = None,
    scope: str = "cross_city",
) -> DeepResearchResult:
    """执行实时深度研究；命中缓存直接返回；超时/失败降级。返回入库 activity ids + 统计。

    scope="local"：市内模式（DD-18），研究目标围绕城内当周主题活动；缓存键纳入 scope。
    """
    operation_started = time.monotonic()
    s = get_settings()
    semantic_constraints = {
        "interests": interests or categories or [],
        "research_goal": research_goal,
        "acceptance_criteria": acceptance_criteria or [],
        "research_subgoals": research_subgoals or [],
    }
    brief = build_brief(
        city_code,
        weekend,
        categories,
        nl_query,
        interests,
        research_goal=research_goal_from_constraints(
            semantic_constraints, feedback=feedback
        ),
        acceptance_criteria=acceptance_criteria_from_constraints(
            semantic_constraints
        ),
        research_subgoals=research_subgoals,
        scope=scope,
    )
    brief["follow_up_queries"] = list(follow_up_queries or [])
    brief["feedback"] = (feedback or "").strip()
    brief["exclude_ids"] = sorted(set(exclude_ids or []))
    qh = _query_hash(
        city_code, weekend, categories, nl_query,
        interests=interests,
        follow_up_queries=follow_up_queries,
        feedback=feedback,
        exclude_ids=exclude_ids,
        research_goal=research_goal,
        acceptance_criteria=acceptance_criteria,
        research_subgoals=research_subgoals,
        scope=scope,
    )
    own = session is None
    sess = session or SessionLocal()
    job = None

    def _cb(t) -> None:
        if on_progress:
            phase, msg, found = t
            on_progress(ProgressEvent(phase, msg, found))

    try:
        # ① 防抖缓存
        cached = None if force_refresh else sess.get(DeepResearchCache, qh)
        cached_meta = dict(cached.source_list or {}) if cached else {}
        cached_candidates = list(cached_meta.get("candidates") or [])
        if (
            cached
            and (cached.result_ids or cached_candidates)
            and cached.expires_at
            and cached.expires_at > _now()
        ):
            return DeepResearchResult(
                activity_ids=list(cached.result_ids or []),
                candidates=cached_candidates,
                source_count=int(cached_meta.get("source_count") or 0),
                official_count=int(cached_meta.get("official_count") or 0),
                query_count=int(cached_meta.get("query_count") or 0),
                round_count=int(cached_meta.get("round_count") or 0),
                coverage=float(cached_meta.get("coverage") or 0.0),
                marginal_gain=float(cached_meta.get("marginal_gain") or 0.0),
                cache_hit=True,
                status="succeeded",
                termination="cache_hit",
                trace=dict(cached_meta.get("trace") or {}),
                provider_status=str(cached_meta.get("provider_status") or "ok"),
                provider_errors=list(cached_meta.get("provider_errors") or []),
                elapsed_s=time.monotonic() - operation_started,
            )
        if on_progress:
            on_progress(ProgressEvent("plan", "正在规划研究方向…"))

        # ② 作业记录（可观测/幂等）
        numeric_plan_id = int(plan_id) if str(plan_id or "").isdigit() else None
        job = DeepResearchJob(
            plan_id=numeric_plan_id, trigger=trigger, query=brief, status="running"
        )
        sess.add(job)
        sess.flush()

        # ③ 有界迭代研究环
        loop_result = run_research_loop(
            brief,
            sess,
            on_progress=_cb,
            allow_fetch=allow_fetch,
            time_budget_s=time_budget_s,
        )
        if hasattr(loop_result, "activity_ids"):
            ids = list(loop_result.activity_ids)
            candidates = list(getattr(loop_result, "candidates", []) or [])
            src_count = int(loop_result.source_count)
            off_count = int(loop_result.official_count)
            termination = str(loop_result.termination)
            diagnostics = list(getattr(loop_result, "diagnostics", []) or [])
            ingest_attempted = int(getattr(loop_result, "ingest_attempted", 0))
            ingest_empty = int(getattr(loop_result, "ingest_empty_count", 0))
            ingest_errors = int(getattr(loop_result, "ingest_error_count", 0))
            ingest_skipped = int(getattr(loop_result, "ingest_skipped_count", 0))
            query_count = int(getattr(loop_result, "query_count", 0))
            round_count = int(getattr(loop_result, "round_count", 0))
            coverage = float(getattr(loop_result, "coverage", 0.0))
            marginal_gain = float(getattr(loop_result, "marginal_gain", 0.0))
            trace = dict(getattr(loop_result, "trace", {}) or {})
            provider_status = str(
                getattr(loop_result, "provider_status", "ok") or "ok"
            )
            provider_errors = list(
                getattr(loop_result, "provider_errors", []) or []
            )
        else:  # 兼容测试/扩展方仍返回旧三元组
            ids, src_count, off_count = loop_result
            candidates = []
            termination = "completed"
            diagnostics = []
            ingest_attempted = ingest_empty = ingest_errors = ingest_skipped = 0
            query_count = round_count = 0
            coverage = marginal_gain = 0.0
            trace = {}
            provider_status = "ok"
            provider_errors = []
        excluded = set(exclude_ids or [])
        ids = list(dict.fromkeys(i for i in ids if i not in excluded))

        if termination == "provider_unavailable":
            status = "failed"
        elif termination == "timeout":
            status = "partial" if ids else "timeout"
        elif ids or candidates:
            status = "succeeded"
        else:
            status = "no_results"
        degraded = status != "succeeded"

        # ④ 只缓存可行动结果；空/失败结果不得阻塞后续“再搜”
        ttl = s.deep_research_cache_ttl_s
        if ttl > 0 and (ids or candidates) and status in {"succeeded", "partial"}:
            sess.merge(DeepResearchCache(
                query_hash=qh, result_ids=ids,
                source_list={
                    "activity_count": len(ids),
                    "source_count": src_count,
                    "official_count": off_count,
                    "query_count": query_count,
                    "round_count": round_count,
                    "coverage": coverage,
                    "marginal_gain": marginal_gain,
                    "candidates": candidates,
                    "trace": trace,
                    "provider_status": provider_status,
                    "provider_errors": provider_errors,
                },
                expires_at=_now() + timedelta(seconds=ttl),
            ))

        job.found_activity_ids = ids
        job.source_count = src_count
        job.official_count = off_count
        job.status = status
        job.query = {**dict(job.query or {}), "trace": trace}
        if provider_errors:
            job.error = json.dumps(
                {
                    "type": "provider_unavailable",
                    "provider_status": provider_status,
                    "errors": provider_errors[:10],
                },
                ensure_ascii=False,
                default=str,
            )[:2000]
        elif (
            diagnostics
            or ingest_empty
            or ingest_errors
            or ingest_skipped
            or (not ids and ingest_attempted)
        ):
            summary = (
                f"ingest attempted={ingest_attempted}, empty={ingest_empty}, "
                f"errors={ingest_errors}, timed_out={ingest_skipped}"
            )
            job.error = "; ".join([summary, *diagnostics])[:2000]
        job.finished_at = _now()
        if own:
            sess.commit()
        return DeepResearchResult(
            activity_ids=ids, candidates=candidates,
            official_count=off_count, source_count=src_count,
            degraded=degraded, job_id=job.id, status=status, termination=termination,
            query_count=query_count, round_count=round_count,
            coverage=coverage, marginal_gain=marginal_gain,
            trace=trace,
            provider_status=provider_status,
            provider_errors=provider_errors,
            elapsed_s=time.monotonic() - operation_started,
        )
    except Exception as exc:
        # caller-owned session 也必须把 job 从 running 推进到终态；否则会制造“僵尸作业”。
        try:
            if job is not None:
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"[:2000]
                job.finished_at = _now()
                if own:
                    sess.commit()
            elif own:
                sess.rollback()
        except Exception:
            if own:
                try:
                    sess.rollback()
                except Exception:
                    pass
        return DeepResearchResult(
            degraded=True,
            job_id=getattr(job, "id", None),
            status="failed",
            termination="error",
            provider_status="unavailable",
            provider_errors=[{
                "type": type(exc).__name__,
                "detail": str(exc)[:1000],
            }],
            elapsed_s=time.monotonic() - operation_started,
        )
    finally:
        if own:
            sess.close()
