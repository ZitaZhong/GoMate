"""DD-17 Research：有界迭代研究环（Supervisor + 并行执行 + CRAG 反思补缺）。

v2 并行版：Phase 1 并行搜索所有子主题 + Phase 2 并行入库所有结果；
受 CONCURRENCY / TIME_BUDGET / MAX_ROUNDS 约束；
search/fetch 经 DD-04/DD-06；无 key → 快速收敛空结果（degraded）。
每线程独立 session（ingest_content session=None），主线程 session 仅做 job 记录。
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..enums import SourceType, VerificationStatus
from ..intel.ingest import ingest_content, ingest_realtime
from ..intel.research import is_official_like
from ..orchestration.guard import status_rank
from ..providers import call as provider_call
from ..retrieval import Weekend
from .semantics import extract_open_candidates, plan_research_tasks

logger = logging.getLogger("uvicorn.error")


@dataclass
class ResearchLoopResult:
    activity_ids: list[int] = field(default_factory=list)
    source_count: int = 0
    official_count: int = 0
    termination: str = "completed"  # completed|converged|no_sources|timeout
    ingest_attempted: int = 0
    ingest_empty_count: int = 0
    ingest_error_count: int = 0
    ingest_skipped_count: int = 0
    diagnostics: list[str] = field(default_factory=list)
    query_count: int = 0
    round_count: int = 0
    coverage: float = 0.0
    marginal_gain: float = 0.0
    candidates: list[dict] = field(default_factory=list)
    trace: dict = field(default_factory=dict)
    provider_status: str = "ok"
    provider_errors: list[dict] = field(default_factory=list)


def _bounded_parallel(
    items,
    fn,
    *,
    max_workers: int,
    deadline: float,
    on_complete=None,
    on_wait=None,
    heartbeat_s: float = 15.0,
):
    """在总 deadline 内逐项收集；返回结果与未完成数，不反向无限等待。

    旧实现用 ``wait(..., ALL_COMPLETED)``，导致已经完成的任务也要等整批结束
    才能向 SSE 上报，用户会长时间看到“开始核实”却没有任何进度。
    """
    if not items:
        return [], 0
    executor = ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(items))))
    futures = [executor.submit(fn, item) for item in items]
    pending = set(futures)
    out = []
    last_heartbeat = time.monotonic()
    while pending:
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            break
        heartbeat_wait = max(0.01, heartbeat_s - (now - last_heartbeat))
        done, pending = wait(
            pending,
            timeout=min(1.0, remaining, heartbeat_wait),
            return_when=FIRST_COMPLETED,
        )
        for future in done:
            try:
                result = future.result()
            except Exception:
                result = None
            out.append(result)
            if on_complete:
                on_complete(len(out), len(items), result)
        now = time.monotonic()
        if pending and on_wait and now - last_heartbeat >= heartbeat_s:
            on_wait(len(out), len(items))
            last_heartbeat = now
    for future in pending:
        future.cancel()
    # 运行中的网络调用无法由 Future 强杀；provider 自身超时负责最终回收。
    # 此处不得 wait=True，否则请求会突破 deep_research_time_budget_s。
    executor.shutdown(wait=False, cancel_futures=True)
    return out, len(pending)


def _city_name(session: Session, city_code: str) -> str:
    return session.scalar(
        text("SELECT name FROM city_playbook WHERE city_code = :c"), {"c": city_code}
    ) or city_code


def _weekend_from_brief(brief: dict) -> Weekend | None:
    w = brief.get("weekend") or {}
    try:
        if w.get("start"):
            return Weekend(datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"]))
    except (ValueError, TypeError):
        return None
    return None


def _label(weekend) -> str:
    s = getattr(weekend, "start", None) if weekend else None
    return f"{s:%m月%d日}周末" if s else "本周末"


def _upgrade_verified(session: Session, act, verify_url: str, *, official: bool) -> None:
    """Phase 3 交叉验证通过 → 升级证据六态（DD-03 §4：search 仅找入口，核实后才可升）。

    只升不降：unknown/estimated → public_source_observed；第二来源为官方白名单源时
    → official_source_confirmed（来源随之切换为该官方源，否则过不了 Guard 来源校验）。
    核实路径记入 evidence.note（可审计）；未核实的活动保持 unknown，不进可信召回（设计本意）。
    """
    target = (VerificationStatus.official_source_confirmed if official
              else VerificationStatus.public_source_observed)
    ev = dict(act.evidence or {})
    if status_rank(ev.get("verification_status") or "unknown") >= status_rank(target):
        return  # 已不低于目标态 → 不降级、不重复标注
    old_url = ev.get("source_url")
    ev["verification_status"] = target.value
    ev["confidence"] = max(float(ev.get("confidence") or 0.0), 0.85 if official else 0.6)
    if official:
        ev["source_type"] = SourceType.official_venue.value
        ev["source_url"] = verify_url
    note = (f"交叉核实：入口 {old_url} → 第二来源 {verify_url}"
            f"（{'官方' if official else '公开'}），升级为 {target.value}")
    ev["note"] = (ev["note"] + "；" if ev.get("note") else "") + note
    act.evidence = ev
    act.verification_status = target.value
    session.flush()


def run_research_loop(
    brief: dict,
    session: Session,
    on_progress=None,
    allow_fetch=None,
    time_budget_s: float | None = None,
):
    """并行执行有界迭代研究；返回带终止原因的 ResearchLoopResult。

    Phase 1：所有子主题并行搜索（Tavily）。
    Phase 2：所有搜索结果并行入库（LLM extract + embed，每线程独立 session）。
    deadline 作为兜底安全网（正常并行应在预算内完成）。
    """
    s = get_settings()
    city = brief["city_code"]
    weekend = _weekend_from_brief(brief)
    city_name = _city_name(session, city)
    brief = {**brief, "city_name": city_name}
    follow_up = [
        str(value).strip()
        for value in (brief.get("follow_up_queries") or [])
        if str(value).strip()
    ]
    available_tools = {}
    if s.search_api_key:
        available_tools["web_search"] = (
            "Search the open web for current, source-backed information."
        )
    if s.amap_key:
        available_tools["map_places"] = (
            "Search a map/POI provider for named places and locations."
        )
    # Direct service callers and offline provider adapters may supply search
    # capability without configuring the production API key.  The provider
    # layer still fails safely when no implementation is available.
    if not available_tools:
        available_tools["web_search"] = (
            "Search the open web for current, source-backed information."
        )
    planning_brief = {
        **brief,
        "research_gaps": follow_up,
        "available_tools": available_tools,
    }
    tasks = plan_research_tasks(
        planning_brief,
        max_tasks=s.deep_research_max_subagents,
    )
    tasks = [
        {
            **task,
            "task_id": str(task.get("task_id") or f"research_task_{index + 1}"),
        }
        for index, task in enumerate(
            tasks[:max(1, s.deep_research_max_subagents)]
        )
    ]
    trace: dict = {
        "tasks": [dict(task) for task in tasks],
        "rounds": [],
        "available_tools": sorted(available_tools),
    }
    logger.info(
        "research_plan city=%s task_count=%d tasks=%s",
        city_name,
        len(tasks),
        [
            {"tool": task.get("tool"), "query": task.get("query")}
            for task in tasks
        ],
    )
    concurrency = min(s.deep_research_concurrency, s.deep_research_max_subagents)
    effective_budget = (
        float(time_budget_s)
        if time_budget_s is not None
        else float(s.deep_research_time_budget_s)
    )
    deadline = time.monotonic() + max(0.05, effective_budget)
    t0 = time.monotonic()

    # 预估时长（基于实测：每 source 平均 ~80s 串行，并行 /concurrency）
    estimated_min = max(1, int((len(tasks) * 4 / concurrency * 80 + 30) // 60) + 1)
    if on_progress:
        on_progress(("plan", f"深度研究中（预计约{estimated_min}分钟，{len(tasks)}个研究任务并行执行）", 0))

    all_ids: list[int] = []
    seen: set[int] = set()
    fetched_urls: set[str] = set()
    source_count = 0
    official_ids: set[int] = set()
    ingest_attempted = 0
    ingest_empty_count = 0
    ingest_error_count = 0
    ingest_skipped_count = 0
    diagnostics: list[str] = []
    timed_out = False
    termination = "completed"
    query_count = 0
    round_count = 0
    coverage = 0.0
    marginal_gain = 0.0
    open_candidates: list[dict] = []
    provider_errors: list[dict] = []

    for rnd in range(s.deep_research_max_rounds):
        if time.monotonic() >= deadline:
            timed_out = True
            break
        round_count = rnd + 1
        round_ids: list[int] = []
        candidates_before_round = len(open_candidates)
        round_trace: dict = {
            "round": round_count,
            "queries": [],
            "sources": [],
            "candidate_extraction": [],
        }
        trace["rounds"].append(round_trace)

        # ═══════ Phase 1: 并行搜索所有子主题 ═══════
        search_results: dict[str, list[dict]] = {}

        def _search_one(task: dict):
            query = str(task.get("query") or "").strip()
            tool = str(task.get("tool") or "web_search")
            angle = "" if rnd == 0 else f" 第{rnd + 1}轮补充 未推荐过"
            if tool == "map_places":
                result = provider_call(
                    "amap",
                    "poi_search",
                    {"keyword": query, "city": city, "limit": 10},
                )
                return task, result, None
            # Open-web tasks use two evidence angles.  The task query is authored
            # by the model from the full brief, not assembled from a topic enum.
            r1 = provider_call(
                "search",
                "web_search",
                {"query": f"{city_name} {_label(weekend)} {query}{angle}", "count": 4},
            )
            target_year = getattr(getattr(weekend, "start", None), "year", None)
            target_year = target_year or datetime.now().year
            r2 = provider_call(
                "search",
                "web_search",
                {
                    "query": (
                        f"{city_name} {query} 官方 来源 时间 地点 "
                        f"{target_year}{angle}"
                    ),
                    "count": 3,
                },
            )
            return task, r1, r2

        query_count += sum(
            1 if task.get("tool") == "map_places" else 2 for task in tasks
        )
        search_out, search_pending = _bounded_parallel(
            tasks,
            _search_one,
            max_workers=concurrency,
            deadline=deadline,
            on_complete=(
                lambda done, total, _result: on_progress((
                    "search",
                    f"检索完成 {done}/{total} 子主题（第{rnd + 1}轮）",
                    len(all_ids),
                ))
                if on_progress else None
            ),
            on_wait=(
                lambda done, total: on_progress((
                    "search",
                    f"仍在检索外部来源… 已完成 {done}/{total} 子主题（第{rnd + 1}轮）",
                    len(all_ids),
                ))
                if on_progress else None
            ),
        )
        timed_out = timed_out or bool(search_pending)
        for item in search_out:
            if not item:
                continue
            task, r1, r2 = item
            query = str(task.get("query") or "").strip()
            if task.get("tool") == "map_places":
                pois = []
                if r1 and r1.ok:
                    pois = (r1.data or {}).get("pois", [])
                    source_count += len(pois)
                    for poi in pois:
                        name = str(poi.get("name") or "").strip()
                        if not name:
                            continue
                        open_candidates.append({
                            "id": None,
                            "title": name,
                            "venue": poi.get("address") or city_name,
                            "category": str(task.get("purpose") or "地点"),
                            "candidate_kind": str(task.get("purpose") or "地点"),
                            "candidate_type": "open_candidate",
                            "description": str(task.get("purpose") or ""),
                            "price_text": None,
                            "booking_url": None,
                            "start_at": None,
                            "end_at": None,
                            "availability_mode": "unknown",
                            "subgoal_ids": list(task.get("subgoal_ids") or []),
                            "research_task_ids": [task["task_id"]],
                            "origin": "current_research",
                            "verification_status": "public_source_observed",
                            "rerank_score": 0.0,
                            "location": poi.get("location"),
                            "evidence": {
                                "source_type": "amap",
                                "source_url": None,
                                "verification_status": "public_source_observed",
                                "confidence": 0.8,
                                "note": "地图 POI 实体；开放时间需另行核实",
                            },
                        })
                round_trace["queries"].append({
                    "query": query,
                    "tool": "map_places",
                    "result_count": len(pois),
                })
                round_trace["sources"].extend([
                    {
                        "query": query,
                        "title": str(poi.get("name") or "")[:300],
                        "url": None,
                        "source_type": "amap",
                    }
                    for poi in pois
                ])
                continue
            all_res = []
            provider_calls = []
            for res in (r1, r2):
                if res is None:
                    continue
                degraded = bool(getattr(res, "degraded", False))
                result_error = getattr(res, "error", None)
                degraded_reason = getattr(res, "degraded_reason", None)
                failed = bool(not res.ok or (degraded and result_error))
                if failed:
                    error = dict(result_error or {})
                    diagnostic = {
                        "provider": error.get("provider") or res.source_type,
                        "status_code": error.get("status_code"),
                        "type": error.get("type") or degraded_reason or "provider_error",
                        "detail": str(error.get("detail") or "")[:500],
                        "retryable": error.get("retryable"),
                        "degraded_reason": degraded_reason,
                    }
                    if diagnostic not in provider_errors:
                        provider_errors.append(diagnostic)
                provider_calls.append({
                    "ok": res.ok,
                    "degraded": degraded,
                    "result_count": len((res.data or {}).get("results") or []),
                    "error": dict(result_error or {}),
                    "degraded_reason": degraded_reason,
                })
                if res and res.ok:
                    all_res += (res.data or {}).get("results", [])
            results = [
                r for r in all_res
                if r.get("url") and r["url"] not in fetched_urls
            ]
            seen_urls = set()
            deduped = []
            for result in results:
                if result["url"] not in seen_urls:
                    seen_urls.add(result["url"])
                    deduped.append(result)
            search_results[query] = deduped
            fetched_urls.update(r["url"] for r in deduped)
            source_count += len(deduped)
            round_trace["queries"].append({
                "query": query,
                "tool": task.get("tool"),
                "result_count": len(deduped),
                "provider_calls": provider_calls,
            })
            round_trace["sources"].extend([
                {
                    "query": query,
                    "title": str(result.get("title") or "")[:300],
                    "url": str(result.get("url") or "")[:1000],
                }
                for result in deduped
            ])
        round_web_calls = [
            call
            for query_trace in round_trace["queries"]
            for call in (query_trace.get("provider_calls") or [])
        ]
        if (
            round_web_calls
            and all(
                not call.get("ok")
                or (call.get("degraded") and call.get("error"))
                for call in round_web_calls
            )
            and not any(search_results.values())
        ):
            termination = "provider_unavailable"
            round_trace["result"] = {
                "new_activity_count": 0,
                "new_candidate_count": 0,
                "coverage": 0.0,
                "marginal_gain": 0.0,
                "provider_status": "unavailable",
            }
            if on_progress:
                on_progress((
                    "error",
                    "外部搜索服务当前不可用，已停止重复检索；这不代表没有符合条件的结果",
                    len(all_ids),
                ))
            break
        if search_pending and time.monotonic() >= deadline:
            termination = "timeout"
            break

        # ═══════ Phase 2: 并行入库所有搜索结果 ═══════
        all_web_items: list[tuple[dict, SourceType]] = []
        for results in search_results.values():
            for r in results:
                st = (
                    SourceType.official_venue
                    if is_official_like(r["url"])
                    else SourceType.search
                )
                all_web_items.append((r, st))

        # Extract each research task independently and in parallel.  One large
        # model call over every source made a single timeout/invalid JSON erase
        # an otherwise successful research run.
        # Keep model payloads small.  Passing 7-10 full Tavily pages to one
        # extraction call regularly exceeded the model timeout and then sent
        # every page into the unrelated legacy Event ingestion pipeline.
        extraction_items = [
            (query, results[offset:offset + 3])
            for query, results in search_results.items()
            for offset in range(0, len(results), 3)
            if results[offset:offset + 3]
        ]
        task_subgoals = {
            str(task.get("query") or "").strip(): list(
                task.get("subgoal_ids") or []
            )
            for task in tasks
        }
        task_ids = {
            str(task.get("query") or "").strip(): str(task.get("task_id") or "")
            for task in tasks
        }

        def _extract_group(item: tuple[str, list[dict]]):
            query, results = item
            started = time.monotonic()
            candidates = extract_open_candidates(results, brief)
            for candidate in candidates:
                if not candidate.get("subgoal_ids"):
                    candidate["subgoal_ids"] = list(
                        task_subgoals.get(query) or []
                    )
                candidate["research_task_ids"] = [
                    task_ids[query]
                ] if task_ids.get(query) else []
                candidate["origin"] = "current_research"
            return {
                "query": query,
                "source_count": len(results),
                "candidate_count": len(candidates),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "candidates": candidates,
            }

        extraction_out, extraction_pending = _bounded_parallel(
            extraction_items,
            _extract_group,
            max_workers=concurrency,
            deadline=deadline,
        )
        timed_out = timed_out or bool(extraction_pending)
        extracted_candidates: list[dict] = []
        for extraction in extraction_out:
            if not extraction:
                continue
            extracted_candidates.extend(extraction.pop("candidates", []) or [])
            round_trace["candidate_extraction"].append(extraction)
        if extraction_pending:
            round_trace["candidate_extraction"].append({
                "status": "timeout",
                "pending_groups": extraction_pending,
            })

        known = {
            (
                "".join(str(item.get("title") or "").lower().split()),
                ((item.get("evidence") or {}).get("source_url")),
            )
            for item in open_candidates
        }
        for candidate in extracted_candidates:
            key = (
                "".join(str(candidate.get("title") or "").lower().split()),
                ((candidate.get("evidence") or {}).get("source_url")),
            )
            if key not in known:
                known.add(key)
                open_candidates.append(candidate)

        # The legacy ingestion pipeline requires a dated Activity.  Only feed
        # it sources that the open extractor identified as dated occurrences.
        # Place/route/always-open candidates remain first-class research
        # candidates instead of being forced through an Event schema.
        dated_urls = {
            str((candidate.get("evidence") or {}).get("source_url") or "")
            for candidate in extracted_candidates
            if candidate.get("availability_mode") == "dated"
        }
        explicit_open_research = bool(brief.get("research_subgoals"))
        if explicit_open_research:
            # The agent-native path keeps dated occurrences and always-open
            # places as first-class research candidates.  Re-ingesting those
            # same pages into the legacy Activity table duplicates extraction,
            # can take minutes, and is not needed for evidence-grounded
            # response composition.
            to_ingest = []
        elif extracted_candidates:
            to_ingest = [
                item
                for item in all_web_items
                if str(item[0].get("url") or "") in dated_urls
            ]
        else:
            to_ingest = all_web_items
        round_trace["legacy_ingest"] = {
            "selected_source_count": len(to_ingest),
            "skipped_non_dated_source_count": len(all_web_items) - len(to_ingest),
            "fallback_all_sources": (
                not bool(extracted_candidates) and not explicit_open_research
            ),
        }

        if on_progress and to_ingest:
            on_progress(("ingest", f"开始并行核实入库 {len(to_ingest)} 条…", len(all_ids)))

        def _ingest_one(item: tuple[dict, SourceType]):
            r, st = item
            content = r.get("content")
            try:
                if content and len(content) >= 80:
                    ids = ingest_content(
                        r["url"], content, city, source_type=st,
                        session=None, weekend=weekend, raise_on_error=True,
                    )
                else:
                    ids = ingest_realtime(
                        [r["url"]], city, weekend, source_type=st,
                        session=None, allow_fetch=allow_fetch,
                    )
                return ids, st, ("empty_extract" if not ids else None)
            except Exception as exc:
                return [], st, f"{type(exc).__name__}: {exc}"

        ingested = 0
        submit_items = [item for item in to_ingest if time.monotonic() < deadline]
        not_submitted = len(to_ingest) - len(submit_items)
        def _ingest_progress(done: int, total: int, _result) -> None:
            if on_progress and (done % 2 == 0 or done == total):
                on_progress((
                    "ingest",
                    f"核实 {done}/{total} 个来源",
                    len(all_ids),
                ))

        ingest_out, ingest_pending = _bounded_parallel(
            submit_items,
            _ingest_one,
            max_workers=concurrency,
            deadline=deadline,
            on_complete=_ingest_progress,
            on_wait=(
                lambda done, total: on_progress((
                    "ingest",
                    f"仍在核实外部来源… 已完成 {done}/{total}",
                    len(all_ids),
                ))
                if on_progress else None
            ),
        )
        timed_out = timed_out or bool(ingest_pending)
        round_empty = 0
        round_errors = 0
        for item in ingest_out:
            if item:
                ids, source_type, diagnostic = item
                round_ids += (ids or [])
                if source_type == SourceType.official_venue:
                    official_ids.update(ids or [])
                if diagnostic == "empty_extract":
                    round_empty += 1
                elif diagnostic:
                    round_errors += 1
                    if diagnostic not in diagnostics and len(diagnostics) < 5:
                        diagnostics.append(diagnostic)
            ingested += 1
        ingest_attempted += ingested
        ingest_empty_count += round_empty
        ingest_error_count += round_errors
        round_skipped = not_submitted + ingest_pending
        ingest_skipped_count += round_skipped

        if on_progress and to_ingest:
            on_progress(("ingest",
                         f"本轮核实完成：新增 {len(set(round_ids))} 项；"
                         f"未抽取 {round_empty} 源；失败 {round_errors} 源；"
                         f"超时未完成 {round_skipped} 源",
                         len(all_ids) + len(round_ids)))

        new_ids = [i for i in round_ids if i not in seen]
        seen.update(round_ids)
        all_ids += new_ids
        new_candidate_count = len(open_candidates) - candidates_before_round
        covered_web = len([v for v in search_results.values() if v])
        covered_map = sum(
            1 for task in tasks
            if task.get("tool") == "map_places" and open_candidates
        )
        coverage = (covered_web + covered_map) / max(1, len(tasks))
        marginal_gain = (
            len(new_ids) + max(0, new_candidate_count)
        ) / max(1, source_count)
        round_trace["result"] = {
            "new_activity_count": len(new_ids),
            "new_candidate_count": max(0, new_candidate_count),
            "coverage": coverage,
            "marginal_gain": marginal_gain,
        }
        logger.info(
            "research_round city=%s round=%d sources=%d candidates=%d "
            "activities=%d coverage=%.3f",
            city_name,
            round_count,
            len(round_trace["sources"]),
            max(0, new_candidate_count),
            len(new_ids),
            coverage,
        )
        if not new_ids and not new_candidate_count and rnd:
            termination = "converged"
            break

        # ═══════ Phase 3: 交叉验证（对入库活动搜第二来源确认日期） ═══════
        if time.monotonic() < deadline and new_ids:
            verify_ids = new_ids[:5]  # 只验前5个（控制API调用量）
            if on_progress:
                on_progress(("verify", f"交叉验证 {len(verify_ids)} 个活动的日期...", len(all_ids)))

            def _verify_one(act_id: int) -> None:
                try:
                    from ..db.session import get_session as _gs
                    with _gs() as vs:
                        from ..models import Activity as _A
                        act = vs.query(_A).filter_by(id=act_id).one_or_none()
                        if not act:
                            return
                        # 用活动标题精搜第二来源
                        verify_year = (
                            getattr(getattr(weekend, "start", None), "year", None)
                            or datetime.now().year
                        )
                        vr = provider_call("search", "web_search",
                                          {"query": f"{act.title} {city_name} {verify_year} 时间 地点", "count": 2})
                        if not vr.ok:
                            return
                        for vres in (vr.data or {}).get("results", [])[:2]:
                            vc = vres.get("content", "")
                            vurl = vres.get("url")
                            if not (vurl and vc and len(vc) >= 80
                                    and vurl != (act.evidence or {}).get("source_url")):
                                continue
                            # 第二独立来源核实通过 → 入库（内部触发dedup+日期对比）
                            # 并按 DD-03 §4 升级原活动证据态（官方白名单源可升 official）
                            v_official = is_official_like(vurl)
                            ingest_content(vurl, vc, city,
                                           source_type=(SourceType.official_venue if v_official
                                                        else SourceType.search),
                                           session=None)
                            _upgrade_verified(vs, act, vurl, official=v_official)
                except Exception:
                    pass

            _, verify_pending = _bounded_parallel(
                verify_ids, _verify_one, max_workers=concurrency, deadline=deadline
            )
            timed_out = timed_out or bool(verify_pending)

        if time.monotonic() >= deadline:
            timed_out = True
            break
        if (new_ids or new_candidate_count) and coverage >= s.deep_research_min_coverage:
            termination = "completed"
            break
        # 覆盖不足时改变搜索角度，禁止原样重复同一批查询。
        tasks = [
            {
                **task,
                "query": f"{task['query']} 补充来源",
            }
            for task in tasks
        ]

    elapsed = int(time.monotonic() - t0)
    if timed_out:
        termination = "timeout"
    elif termination == "provider_unavailable":
        pass
    elif source_count == 0 and not open_candidates:
        termination = "no_sources"
    if on_progress:
        message = (
            "深研未完成：外部搜索服务不可用，未将服务故障解释为“没有结果”"
            if termination == "provider_unavailable"
            else (
                f"深研完成：获得 {len(open_candidates)} 个开放候选，"
                f"入库 {len(all_ids)} 条日期型活动（耗时 {elapsed}s）"
            )
        )
        on_progress((
            "done",
            message,
            len(open_candidates) + len(all_ids),
        ))
    trace["summary"] = {
        "source_count": source_count,
        "candidate_count": len(open_candidates),
        "activity_count": len(all_ids),
        "termination": termination,
        "elapsed_s": elapsed,
        "provider_status": (
            "unavailable" if termination == "provider_unavailable" else "ok"
        ),
        "provider_errors": provider_errors[:10],
    }
    return ResearchLoopResult(
        activity_ids=all_ids,
        candidates=open_candidates,
        source_count=source_count,
        official_count=len(official_ids),
        termination=termination,
        ingest_attempted=ingest_attempted,
        ingest_empty_count=ingest_empty_count,
        ingest_error_count=ingest_error_count,
        ingest_skipped_count=ingest_skipped_count,
        diagnostics=diagnostics,
        query_count=query_count,
        round_count=round_count,
        coverage=coverage,
        marginal_gain=marginal_gain,
        trace=trace,
        provider_status=(
            "unavailable" if termination == "provider_unavailable" else "ok"
        ),
        provider_errors=provider_errors[:10],
    )
