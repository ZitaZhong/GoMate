"""多轮 Deep Research 端到端验收。

覆盖四层：研究服务语义缓存/作业终态、时间召回、LangGraph 真回环、
BFF checkpoint 版本透传，以及截图中的“本轮空但摘要仍有旧 5 项”回归。
全部离线、确定性，不调用外部搜索或 LLM。
"""
from __future__ import annotations

import importlib
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from wheretogo.intel.extract import extract_activities
from wheretogo.intel.research import is_official_like
from wheretogo.bff.app import app
from wheretogo.db import get_session
from wheretogo.models import Plan
from wheretogo.models import DeepResearchCache, DeepResearchJob
from wheretogo.orchestration.bundle import compose_explore_bundle
from wheretogo.orchestration.graph import PlannerService, route_after_reflect
from wheretogo.orchestration.nodes import (
    _candidate_available,
    _distinct_activity_entities,
    _in_window,
    _matches_constraint_kinds,
    _personalize_activities,
    _title_year_matches_dates,
    activity_reflection,
)
from wheretogo.orchestration.state import TripPlanState
from wheretogo.research.service import _query_hash, deep_research
from wheretogo.research.supervisor import (
    ResearchLoopResult,
    _bounded_parallel,
    run_research_loop,
)
from wheretogo.retrieval import Weekend
from wheretogo.retrieval.recall import structured_recall


def _wk() -> Weekend:
    return Weekend(
        datetime(2026, 7, 25, tzinfo=timezone.utc),
        datetime(2026, 7, 27, tzinfo=timezone.utc),
    )


def test_bounded_parallel_streams_completions_and_reports_pending_work():
    observed: list[tuple[int, int, float]] = []

    def work(delay: float) -> float:
        time.sleep(delay)
        return delay

    out, pending = _bounded_parallel(
        [0.01, 0.2],
        work,
        max_workers=2,
        deadline=time.monotonic() + 0.06,
        on_complete=lambda done, total, result: observed.append((done, total, result)),
        heartbeat_s=0.01,
    )
    assert out == [0.01]
    assert pending == 1
    assert observed == [(1, 2, 0.01)]


def test_bounded_parallel_emits_heartbeat_before_a_slow_item_finishes():
    heartbeats: list[tuple[int, int]] = []
    out, pending = _bounded_parallel(
        [0.08],
        lambda delay: (time.sleep(delay), delay)[1],
        max_workers=1,
        deadline=time.monotonic() + 0.2,
        on_wait=lambda done, total: heartbeats.append((done, total)),
        heartbeat_s=0.02,
    )
    assert out == [0.08]
    assert pending == 0
    assert heartbeats
    assert all(item == (0, 1) for item in heartbeats)


def test_window_filter_rejects_naive_old_dates_and_keeps_overlapping_exhibitions():
    assert _in_window("2023-08-29T10:00:00", _wk(), "2023-08-29T18:00:00") is False
    assert _in_window("2026-06-01T10:00:00", _wk(), "2026-08-30T18:00:00") is True
    assert _in_window("2023-08-29T10:00:00", _wk(), "2026-08-29T18:00:00") is False
    assert _in_window("not-a-date", _wk()) is False


def test_open_candidate_uses_its_own_availability_semantics():
    assert _candidate_available({
        "candidate_type": "open_candidate",
        "availability_mode": "always",
        "start_at": None,
        "end_at": None,
    }, _wk()) is True
    assert _candidate_available({
        "candidate_type": "open_candidate",
        "availability_mode": "dated",
        "start_at": "2021-08-07T10:00:00+08:00",
        "end_at": "2021-08-07T18:00:00+08:00",
    }, _wk()) is False


def test_title_year_must_match_extracted_occurrence_year():
    assert _title_year_matches_dates(
        "杭州·2026某歌手巡演", "2026-07-25T19:30:00+08:00"
    ) is True
    assert _title_year_matches_dates(
        "2025梦想天堂演唱会", "2026-08-01T19:30:00+08:00"
    ) is False
    assert _title_year_matches_dates(
        "无年份的长期艺术展",
        "2026-06-01T10:00:00+08:00",
        "2026-08-01T18:00:00+08:00",
    ) is True


def test_legacy_kind_filter_delegates_to_semantic_judge(monkeypatch):
    monkeypatch.setattr(
        "wheretogo.research.semantics.evaluate_candidates",
        lambda candidates, **_kwargs: (
            candidates if candidates[0]["title"] == "开放候选A" else [],
            {"evaluated": True, "matched_count": 1, "criterion_coverage": 1.0},
        ),
    )
    assert _matches_constraint_kinds(
        {"title": "开放候选A"}, ["源码中从未出现的需求"]
    ) is True
    assert _matches_constraint_kinds(
        {"title": "开放候选B"}, ["源码中从未出现的需求"]
    ) is False


def test_open_place_entities_are_deduplicated_by_exact_normalized_title():
    candidates = [
        {
            "title": "宝石山",
            "evidence": {"source_url": "https://example.test/a"},
        },
        {
            "title": " 宝 石 山 （杭州西湖）",
            "evidence": {"source_url": "https://example.test/b"},
        },
        {
            "title": "九溪十八涧",
            "evidence": {"source_url": "https://example.test/c"},
        },
    ]

    result = _distinct_activity_entities(candidates)

    assert [item["title"] for item in result] == ["宝石山", "九溪十八涧"]


def test_reflection_stops_after_source_candidates_when_semantic_judge_is_down():
    candidate = {
        "title": "九溪十八涧",
        "candidate_type": "open_candidate",
        "evidence": {"source_url": "https://example.test/jiuxi"},
    }

    reflected = activity_reflection({
        "activities": [candidate],
        "constraints": {
            "research_goal": "杭州自然景点",
            "acceptance_criteria": ["候选本身是可到访的自然地点"],
        },
        "candidate_cities": [{"name": "杭州"}],
        "research_semantic_evaluation": {
            "evaluated": False,
            "failure": "semantic_judge_unavailable",
        },
        "research_active_feedback": "有没有自然景点",
        "research_loop_count": 1,
    })

    assert reflected["activities"] == [candidate]
    assert reflected["research_should_continue"] is False
    assert reflected["research_stop_reason"] == "semantic_judge_unavailable"
    assert len(reflected["warnings"]) == 1


def test_generic_followup_keeps_original_interest_constraint(monkeypatch):
    """“换一批更小众的”不能丢掉首轮的演唱会偏好并混入展览。"""
    import wheretogo.orchestration.nodes as nodes

    @contextmanager
    def fake_session():
        yield object()

    def candidate(activity_id: int, title: str, category: str):
        return SimpleNamespace(
            id=activity_id,
            title=title,
            venue="测试场馆",
            category=category,
            price_text=None,
            booking_url=None,
            start_at=_wk().start,
            end_at=_wk().start + timedelta(hours=2),
            verification_status="public_source_observed",
            rerank_score=0.5,
            location=None,
            evidence={},
        )

    candidates = [
        candidate(2, "新乐队巡演", "演唱会"),
        candidate(3, "印象派艺术大展", "展览"),
    ]
    monkeypatch.setattr(nodes, "get_session", fake_session)
    monkeypatch.setattr(nodes, "_city_name", lambda *_args: "杭州")
    monkeypatch.setattr(nodes._deep_research, "enabled", lambda: False)
    monkeypatch.setattr(nodes._deep_research, "should_run", lambda: False)
    monkeypatch.setattr(
        nodes._RETRIEVAL,
        "retrieve_activities",
        lambda *_args, **_kwargs: candidates,
    )
    monkeypatch.setattr(
        "wheretogo.research.semantics.evaluate_candidates",
        lambda values, **_kwargs: (
            [item for item in values if item.get("id") != 3],
            {
                "evaluated": True,
                "matched_count": sum(item.get("id") != 3 for item in values),
                "criterion_coverage": 1.0,
                "gaps": [],
            },
        ),
    )

    result = nodes.activity_research({
        "plan_id": "matrix",
        "constraints": {
            "experience_requirements": ["延续上一轮的现场音乐需求"],
            "acceptance_criteria": ["必须满足上一轮需求"],
            "research_goal": "换一批更小众的现场音乐",
            "query": "杭州 演唱会",
            "target_city_code": "330100",
            "weekend_start": _wk().start.isoformat(),
            "weekend_end": _wk().end.isoformat(),
        },
        "candidate_cities": [{"city_code": "330100", "name": "杭州"}],
        "follow_up_queries": ["杭州 更小众的 新活动"],
        "research_active_feedback": "换一批更小众的",
        "research_baseline_activities": [{"id": 1, "title": "上一轮演唱会"}],
        "shown_activity_ids": [1],
        "shown_activity_titles": ["上一轮演唱会"],
    })
    assert [item["title"] for item in result["activities"]] == ["新乐队巡演"]


def test_cache_key_covers_followups_feedback_and_exclusions():
    base = _query_hash("330100", _wk(), ["展览"], "周末艺术")
    assert base != _query_hash(
        "330100", _wk(), ["展览"], "周末艺术",
        follow_up_queries=["杭州 小众新展"],
    )
    assert base != _query_hash(
        "330100", _wk(), ["展览"], "周末艺术", feedback="不要刚才那些",
    )
    assert base != _query_hash(
        "330100", _wk(), ["展览"], "周末艺术", exclude_ids=[1, 2, 3],
    )


def test_empty_result_is_not_cached_and_job_reaches_no_results(session, monkeypatch):
    monkeypatch.setattr(
        "wheretogo.research.service.run_research_loop",
        lambda *a, **k: ResearchLoopResult(
            activity_ids=[], source_count=28, official_count=1, termination="converged",
            ingest_attempted=28, ingest_empty_count=27, ingest_error_count=1,
            ingest_skipped_count=3,
            diagnostics=["AttributeError: broken source"],
        ),
    )
    result = deep_research(
        "empty-case", _wk(), ["展览"], "截图空结果", session=session, plan_id=None
    )
    assert result.status == "no_results"
    assert result.degraded is True
    job = session.get(DeepResearchJob, result.job_id)
    assert job.status == "no_results"
    assert job.finished_at is not None
    assert "attempted=28" in (job.error or "")
    assert "timed_out=3" in (job.error or "")
    assert "broken source" in (job.error or "")
    qh = _query_hash("empty-case", _wk(), ["展览"], "截图空结果")
    assert session.get(DeepResearchCache, qh) is None


def test_candidate_only_result_is_cached_with_trace(session, monkeypatch):
    candidate = {
        "id": None,
        "title": "九溪十八涧",
        "candidate_type": "open_candidate",
        "availability_mode": "always",
        "evidence": {"source_url": "https://example.test/jiuxi"},
    }
    monkeypatch.setattr(
        "wheretogo.research.service.run_research_loop",
        lambda *a, **k: ResearchLoopResult(
            activity_ids=[],
            candidates=[candidate],
            source_count=6,
            termination="completed",
            query_count=2,
            round_count=1,
            coverage=1.0,
            trace={"tasks": [{"query": "杭州九溪", "tool": "web_search"}]},
        ),
    )
    first = deep_research(
        "candidate-cache-case",
        _wk(),
        nl_query="杭州自然景点",
        session=session,
    )
    assert first.status == "succeeded"
    assert first.candidates == [candidate]
    qh = _query_hash(
        "candidate-cache-case",
        _wk(),
        None,
        "杭州自然景点",
    )
    cached = session.get(DeepResearchCache, qh)
    assert cached is not None
    assert cached.result_ids == []
    assert cached.source_list["candidates"] == [candidate]
    job = session.get(DeepResearchJob, first.job_id)
    assert job.query["trace"]["tasks"][0]["query"] == "杭州九溪"


def test_open_place_research_does_not_force_sources_through_event_ingest(monkeypatch):
    import hashlib
    import wheretogo.research.supervisor as supervisor

    tasks = [
        {"query": "杭州九溪十八涧", "tool": "web_search", "purpose": "具体地点"},
        {"query": "杭州西溪湿地", "tool": "web_search", "purpose": "具体地点"},
        {"query": "杭州云栖竹径", "tool": "web_search", "purpose": "具体地点"},
    ]
    monkeypatch.setattr(supervisor, "_city_name", lambda *_args: "杭州")
    monkeypatch.setattr(
        supervisor,
        "plan_research_tasks",
        lambda *_args, **_kwargs: tasks,
    )

    def fake_provider(_provider, _operation, params):
        query = params["query"]
        entity = next(
            value for value in ("九溪十八涧", "西溪湿地", "云栖竹径")
            if value in query
        )
        suffix = hashlib.sha1(query.encode()).hexdigest()[:8]
        return SimpleNamespace(
            ok=True,
            data={"results": [{
                "title": entity,
                "url": f"https://example.test/{entity}/{suffix}",
                "content": f"{entity}是可实际到访的杭州自然去处，页面提供开放信息。",
            }]},
        )

    monkeypatch.setattr(supervisor, "provider_call", fake_provider)

    def fake_extract(sources, _brief):
        source = sources[0]
        return [{
            "id": None,
            "title": source["title"],
            "candidate_type": "open_candidate",
            "candidate_kind": "来源支持的具体去处",
            "availability_mode": "always",
            "start_at": None,
            "end_at": None,
            "booking_url": source["url"],
            "evidence": {"source_url": source["url"]},
        }]

    monkeypatch.setattr(supervisor, "extract_open_candidates", fake_extract)
    monkeypatch.setattr(
        supervisor,
        "ingest_content",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("always-open place source must not enter Event ingest")
        ),
    )
    monkeypatch.setattr(
        supervisor,
        "ingest_realtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("always-open place source must not enter Event ingest")
        ),
    )

    result = run_research_loop(
        {
            "city_code": "330100",
            "research_goal": "推荐杭州自然景点",
            "acceptance_criteria": ["候选本身是具体可到访地点"],
            "weekend": {
                "start": _wk().start.isoformat(),
                "end": _wk().end.isoformat(),
            },
        },
        object(),
        time_budget_s=5,
    )

    assert {item["title"] for item in result.candidates} == {
        "九溪十八涧",
        "西溪湿地",
        "云栖竹径",
    }
    assert result.activity_ids == []
    assert result.ingest_attempted == 0
    assert result.query_count == 6
    assert result.coverage == 1.0
    assert result.trace["summary"]["candidate_count"] == 3
    assert result.trace["rounds"][0]["legacy_ingest"] == {
        "selected_source_count": 0,
        "skipped_non_dated_source_count": 6,
        "fallback_all_sources": False,
    }


def test_provider_outage_stops_after_one_round_and_reports_root_cause(monkeypatch):
    import wheretogo.research.supervisor as supervisor

    monkeypatch.setattr(supervisor, "_city_name", lambda *_args: "杭州")
    monkeypatch.setattr(
        supervisor,
        "plan_research_tasks",
        lambda *_args, **_kwargs: [
            {
                "query": "杭州 周末 演唱会",
                "tool": "web_search",
                "purpose": "find a dated concert",
                "subgoal_ids": ["concert"],
            }
        ],
    )
    monkeypatch.setattr(
        supervisor,
        "provider_call",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=True,
            data={"results": []},
            source_type="search",
            degraded=True,
            degraded_reason="primary_not_ok",
            error={
                "provider": "tavily",
                "status_code": 432,
                "retryable": False,
                "detail": "usage limit",
            },
        ),
    )

    result = run_research_loop(
        {
            "city_code": "330100",
            "research_goal": "周末看演唱会",
            "research_subgoals": [
                {"id": "concert", "objective": "看演唱会", "required": True}
            ],
            "weekend": {
                "start": _wk().start.isoformat(),
                "end": _wk().end.isoformat(),
            },
        },
        object(),
        time_budget_s=5,
    )

    assert result.termination == "provider_unavailable"
    assert result.round_count == 1
    # One semantic task fans out to two evidence angles, but never starts a
    # second research round after the non-retryable provider failure.
    assert result.query_count == 2
    assert result.provider_status == "unavailable"
    assert result.provider_errors[0]["status_code"] == 432


def test_open_research_extractor_failure_does_not_enter_event_pipeline(
    monkeypatch,
):
    import wheretogo.research.supervisor as supervisor

    monkeypatch.setattr(supervisor, "_city_name", lambda *_args: "杭州")
    monkeypatch.setattr(
        supervisor,
        "plan_research_tasks",
        lambda *_args, **_kwargs: [
            {
                "query": "杭州 博物馆",
                "tool": "web_search",
                "purpose": "find visitable museums",
                "subgoal_ids": ["museum"],
            }
        ],
    )
    monkeypatch.setattr(
        supervisor,
        "provider_call",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=True,
            data={
                "results": [
                    {
                        "title": f"source-{index}",
                        "url": f"https://example.test/{index}",
                        "content": "source text " * 20,
                    }
                    for index in range(4)
                ]
            },
            source_type="search",
            degraded=False,
            error=None,
            degraded_reason=None,
        ),
    )
    extraction_batch_sizes: list[int] = []

    def empty_extract(sources, _brief):
        extraction_batch_sizes.append(len(sources))
        return []

    monkeypatch.setattr(supervisor, "extract_open_candidates", empty_extract)
    monkeypatch.setattr(
        supervisor,
        "ingest_content",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("open research must not fall through to Event ingest")
        ),
    )
    monkeypatch.setattr(
        supervisor,
        "ingest_realtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("open research must not fall through to Event ingest")
        ),
    )

    result = run_research_loop(
        {
            "city_code": "330100",
            "research_goal": "杭州周末看博物馆",
            "research_subgoals": [
                {"id": "museum", "objective": "看博物馆", "required": True}
            ],
            "weekend": {
                "start": _wk().start.isoformat(),
                "end": _wk().end.isoformat(),
            },
        },
        object(),
        time_budget_s=5,
    )

    assert sorted(extraction_batch_sizes) == [1, 3]
    assert result.ingest_attempted == 0
    assert result.candidates == []
    assert result.trace["rounds"][0]["legacy_ingest"] == {
        "selected_source_count": 0,
        "skipped_non_dated_source_count": 4,
        "fallback_all_sources": False,
    }


def test_dated_open_candidate_is_not_reingested_into_legacy_event_pipeline(
    monkeypatch,
):
    import wheretogo.research.supervisor as supervisor

    monkeypatch.setattr(supervisor, "_city_name", lambda *_args: "杭州")
    monkeypatch.setattr(
        supervisor,
        "plan_research_tasks",
        lambda *_args, **_kwargs: [
            {
                "query": "杭州 周末 演唱会",
                "tool": "web_search",
                "purpose": "find a dated concert",
                "subgoal_ids": ["concert"],
            }
        ],
    )
    monkeypatch.setattr(
        supervisor,
        "provider_call",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=True,
            data={
                "results": [
                    {
                        "title": "周六音乐现场",
                        "url": "https://example.test/concert",
                        "content": "演出日期与地点证据 " * 20,
                    }
                ]
            },
            source_type="search",
            degraded=False,
            error=None,
            degraded_reason=None,
        ),
    )
    monkeypatch.setattr(
        supervisor,
        "extract_open_candidates",
        lambda *_args, **_kwargs: [
            {
                "title": "周六音乐现场",
                "candidate_type": "open_candidate",
                "availability_mode": "dated",
                "start_at": _wk().start.isoformat(),
                "end_at": _wk().end.isoformat(),
                "evidence": {"source_url": "https://example.test/concert"},
            }
        ],
    )
    monkeypatch.setattr(
        supervisor,
        "ingest_content",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("agent-native candidate must not be reingested")
        ),
    )

    result = run_research_loop(
        {
            "city_code": "330100",
            "research_goal": "杭州周末看演唱会",
            "research_subgoals": [
                {"id": "concert", "objective": "看演唱会", "required": True}
            ],
            "weekend": {
                "start": _wk().start.isoformat(),
                "end": _wk().end.isoformat(),
            },
        },
        object(),
        time_budget_s=5,
    )

    assert result.ingest_attempted == 0
    assert [item["title"] for item in result.candidates] == ["周六音乐现场"]
    assert result.trace["rounds"][0]["legacy_ingest"] == {
        "selected_source_count": 0,
        "skipped_non_dated_source_count": 1,
        "fallback_all_sources": False,
    }


def test_partial_result_records_timed_out_sources(session, monkeypatch):
    monkeypatch.setattr(
        "wheretogo.research.service.run_research_loop",
        lambda *a, **k: ResearchLoopResult(
            activity_ids=[901],
            source_count=4,
            official_count=0,
            termination="timeout",
            ingest_attempted=2,
            ingest_empty_count=1,
            ingest_skipped_count=2,
        ),
    )
    result = deep_research("partial-case", _wk(), ["展览"], session=session)
    assert result.status == "partial"
    job = session.get(DeepResearchJob, result.job_id)
    assert "timed_out=2" in (job.error or "")


def test_failure_finalizes_caller_owned_job(session, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr("wheretogo.research.service.run_research_loop", _boom)
    result = deep_research("failure-case", _wk(), ["演出"], session=session)
    assert result.status == "failed"
    job = session.get(DeepResearchJob, result.job_id)
    assert job.status == "failed"
    assert job.finished_at is not None
    assert "provider exploded" in (job.error or "")


def test_ingest_content_without_caller_session_uses_session_factory(monkeypatch):
    """回归：SessionLocal() 才是 Session；get_session() 返回上下文管理器，不能直接 query。"""
    import wheretogo.intel.ingest as ingest

    fake_session = MagicMock()
    source = SimpleNamespace(id=7, city_code="330100", entry_url="https://example.test")
    monkeypatch.setattr(ingest, "SessionLocal", lambda: fake_session)
    monkeypatch.setattr(ingest, "ensure_source", lambda *a, **k: source)
    monkeypatch.setattr(ingest, "_ingest_drafts_from_md", lambda *a, **k: [901])

    ids = ingest.ingest_content(
        "https://example.test", "活动正文" * 50, "330100", weekend=_wk()
    )

    assert ids == [901]
    fake_session.commit.assert_called_once()
    fake_session.close.assert_called_once()


def test_multiround_bypasses_first_round_cache(session, monkeypatch):
    calls = []

    def _loop(brief, *args, **kwargs):
        calls.append(brief)
        ids = [101] if not brief.get("follow_up_queries") else [202]
        return ResearchLoopResult(ids, 2, 1, "completed")

    monkeypatch.setattr("wheretogo.research.service.run_research_loop", _loop)
    first = deep_research("cache-rounds", _wk(), ["展览"], "艺术", session=session)
    cached = deep_research("cache-rounds", _wk(), ["展览"], "艺术", session=session)
    second = deep_research(
        "cache-rounds", _wk(), ["展览"], "艺术",
        follow_up_queries=["小众艺术 周末限定"],
        exclude_ids=[101],
        force_refresh=True,
        session=session,
    )
    assert first.activity_ids == [101] and cached.cache_hit is True
    assert second.activity_ids == [202] and second.cache_hit is False
    assert len(calls) == 2


def test_ongoing_exhibition_overlaps_weekend(session, make_activity):
    ongoing = make_activity(
        "开展已久但本周末仍开放",
        start_at=_wk().start - timedelta(days=20),
        end_at=_wk().end + timedelta(days=20),
        with_embedding=False,
    )
    ids = structured_recall(session, "310000", _wk().start, _wk().end)
    assert ongoing.id in ids
    assert _in_window(
        ongoing.start_at.isoformat(), _wk(), ongoing.end_at.isoformat()
    ) is True


def _research_reflect_graph(calls: list[dict]):
    """构造只保留 research↔reflect 的真实 LangGraph，隔离外围交通/DB。"""

    def research(state):
        followups = list(state.get("follow_up_queries") or [])
        calls.append({
            "followups": followups,
            "shown": list(state.get("shown_activity_ids") or []),
            "feedback": state.get("research_active_feedback"),
        })
        if not followups:
            return {
                "activities": [
                    {"id": 10 + i, "title": f"首轮活动{10 + i}"} for i in range(3)
                ],
                "research_improved": True,
            }
        if len(calls) == 2:
            # 第一个补搜角度没有改进：保留 baseline，但必须继续换查询，不能以旧结果结束。
            return {
                "activities": list(state.get("research_baseline_activities") or state["activities"]),
                "research_round_candidates": [],
                "research_improved": False,
            }
        return {
            "activities": [
                {"id": 20 + i, "title": f"沉浸式新展{20 + i}"} for i in range(3)
            ],
            "research_round_candidates": [
                {"id": 20 + i, "title": f"沉浸式新展{20 + i}"} for i in range(3)
            ],
            "research_improved": True,
        }

    graph = StateGraph(TripPlanState)
    graph.add_node("research", research)
    graph.add_node("reflect", activity_reflection)
    graph.add_node("finish", lambda state: {"research_should_continue": False})
    graph.add_edge(START, "research")
    graph.add_edge("research", "reflect")
    graph.add_conditional_edges(
        "reflect",
        route_after_reflect,
        {"research": "research", "transport": "finish"},
    )
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=MemorySaver())


def test_true_langgraph_multiround_uses_new_query_and_replaces_old_results(monkeypatch):
    """核心图级 E2E：首轮 A → 用户反馈 → 先反思生成 query → 次轮 B，无重复无旧态。"""
    monkeypatch.setattr("wheretogo.providers.extract_json", lambda *a, **k: None)
    calls: list[dict] = []
    compiled = _research_reflect_graph(calls)
    planner = object.__new__(PlannerService)
    planner.graph = compiled
    thread_id = "plan:graph-e2e:v1"
    cfg = planner._config("graph-e2e", thread_id)
    initial = {
        "plan_id": "graph-e2e",
        "constraints": {"interests": ["展览"]},
        "candidate_cities": [{"city_code": "330100", "name": "杭州"}],
        "activities": [],
        "research_history": [],
        "research_loop_count": 0,
        "research_feedback": None,
        "shown_activity_ids": [],
        "follow_up_queries": [],
        "research_should_continue": False,
        "research_active_feedback": None,
        "research_baseline_activities": [],
        "research_round_candidates": [],
        "research_improved": None,
    }
    first = compiled.invoke(initial, cfg)
    assert [a["id"] for a in first["activities"]] == [10, 11, 12]

    planner.prepare_research_more("graph-e2e", "不喜欢，换一批", thread_id=thread_id)
    second = compiled.invoke(None, cfg)

    assert len(calls) == 3
    assert calls[1]["followups"], "第二次 research 必须真正收到 reflect 生成的新查询"
    assert calls[1]["shown"] == [10, 11, 12]
    assert calls[1]["feedback"] == "不喜欢，换一批"
    assert calls[2]["feedback"] == "不喜欢，换一批"
    assert not (set(calls[2]["followups"]) & set(calls[1]["followups"])), "无改进时必须换搜索角度"
    assert [a["id"] for a in second["activities"]] == [20, 21, 22]
    assert second["research_feedback"] is None
    assert second["research_should_continue"] is False
    assert second["research_outcome"] == "improved"
    assert len(second["research_history"]) == 2


def test_feedback_round_keeps_baseline_when_one_search_angle_is_empty(monkeypatch):
    """补搜暂时为空时保留 baseline，且不把中间轮次误报成最终结论。"""
    import wheretogo.orchestration.nodes as nodes

    baseline = [
        {
            "id": i,
            "title": f"首轮活动{i}",
            "start_at": "2026-07-25T10:00:00+00:00",
            "end_at": "2026-07-25T12:00:00+00:00",
        }
        for i in range(1, 6)
    ]

    @contextmanager
    def _session():
        yield object()

    class _EmptyRetrieval:
        def retrieve_activities(self, *args, **kwargs):
            return []

    monkeypatch.setattr(nodes, "get_session", _session)
    monkeypatch.setattr(nodes, "_city_name", lambda *a: "杭州")
    monkeypatch.setattr(nodes, "_RETRIEVAL", _EmptyRetrieval())
    result = nodes.activity_research({
        "plan_id": "fallback",
        "constraints": {
            "interests": ["沉浸式展览"],
            "weekend_start": "2026-07-25T00:00:00+00:00",
            "weekend_end": "2026-07-27T00:00:00+00:00",
        },
        "candidate_cities": [{"city_code": "330100", "name": "杭州"}],
        "activities": baseline,
        "research_baseline_activities": baseline,
        "research_round_candidates": [],
        "research_active_feedback": "更喜欢沉浸式、小众展览",
        "follow_up_queries": ["杭州 沉浸式 小众展览 本周末"],
        "shown_activity_ids": [1, 2, 3, 4, 5],
    })
    assert result["activities"] == baseline
    assert result["research_improved"] is False
    assert result["research_outcome"] == "searching"
    assert result["warnings"] == []


def test_feedback_recall_fetches_beyond_old_top_ten(monkeypatch):
    """回归：先 top-10 后排除会恒为空；反馈轮必须扩大召回窗口再排旧项。"""
    import wheretogo.orchestration.nodes as nodes

    def _candidate(activity_id):
        return SimpleNamespace(
            id=activity_id,
            title=f"活动{activity_id}",
            venue="杭州场馆",
            category="演唱会",
            price_text=None,
            booking_url=None,
            start_at=datetime(2026, 7, 25, 10, tzinfo=timezone.utc),
            end_at=datetime(2026, 7, 25, 12, tzinfo=timezone.utc),
            verification_status="public_source_observed",
            rerank_score=1 / activity_id,
            location=None,
            evidence={"source_type": "search"},
        )

    @contextmanager
    def _session():
        yield object()

    class _Retrieval:
        top_k = None

        def retrieve_activities(self, *args, **kwargs):
            self.top_k = kwargs["top_k"]
            return [_candidate(i) for i in range(1, 14)]

    retrieval = _Retrieval()
    monkeypatch.setattr(nodes, "get_session", _session)
    monkeypatch.setattr(nodes, "_city_name", lambda *a: "杭州")
    monkeypatch.setattr(nodes, "_RETRIEVAL", retrieval)
    baseline = [{"id": i, "title": f"活动{i}"} for i in range(1, 11)]
    result = nodes.activity_research({
        "constraints": {
            "weekend_start": "2026-07-25T00:00:00+00:00",
            "weekend_end": "2026-07-27T00:00:00+00:00",
        },
        "candidate_cities": [{"city_code": "330100", "name": "杭州"}],
        "activities": baseline,
        "research_baseline_activities": baseline,
        "research_active_feedback": "推荐其他演唱会",
        "follow_up_queries": ["杭州 其他演唱会"],
        "shown_activity_ids": list(range(1, 11)),
    })
    assert retrieval.top_k == 50
    assert [activity["id"] for activity in result["activities"]] == [11, 12, 13]
    assert result["research_improved"] is True


def test_feedback_round_excludes_cross_source_event_variants_and_wrong_kind(monkeypatch):
    """同一演出换来源/换标题/换 ID 仍须排除，“其他演唱会”不能混入景区演出。"""
    import wheretogo.orchestration.nodes as nodes

    def _candidate(activity_id, title, category="演唱会"):
        return SimpleNamespace(
            id=activity_id,
            title=title,
            venue="杭州场馆",
            category=category,
            price_text=None,
            booking_url=None,
            start_at=datetime(2026, 7, 25, 10, tzinfo=timezone.utc),
            end_at=datetime(2026, 7, 25, 12, tzinfo=timezone.utc),
            verification_status="public_source_observed",
            rerank_score=1.0,
            location=None,
            evidence={"source_type": "search"},
        )

    @contextmanager
    def _session():
        yield object()

    class _Retrieval:
        def retrieve_activities(self, *args, **kwargs):
            return [
                _candidate(101, "杭州·2026洛天依「无限共鸣纯蓝幻乐」巡回演唱会杭州站"),
                _candidate(102, "许嵩2026“安泊猜想”巡回演唱会"),
                _candidate(103, "杭州 · 陸虎2026《像你這樣的朋友3.0》巡演"),
                _candidate(104, "【含景区门票+演出】杭州宋城千古情", "其他"),
                _candidate(105, "于贞2026「邀请函」巡演 - 热血说唱"),
                _candidate(106, "于贞2026邀请函巡回演唱会杭州站"),
                _candidate(107, "杭州摇滚新声音乐会"),
                _candidate(108, "杭州站《妄念谋杀》沉浸式爆笑喜剧", "喜剧"),
                _candidate(109, "印象西湖·最忆是杭州大型水上实景演出", "音乐"),
            ]

    monkeypatch.setattr(nodes, "get_session", _session)
    monkeypatch.setattr(nodes, "_city_name", lambda *a: "杭州")
    monkeypatch.setattr(nodes, "_RETRIEVAL", _Retrieval())
    monkeypatch.setattr(nodes._deep_research, "should_run", lambda: False)
    def semantic_filter(values, **_kwargs):
        keep = [
            item for item in values
            if item.get("id") in {1, 2, 3, 105, 107}
        ]
        return keep, {
            "evaluated": True,
            "matched_count": len(keep),
            "criterion_coverage": 1.0,
            "gaps": [],
        }
    monkeypatch.setattr(
        "wheretogo.research.semantics.evaluate_candidates",
        semantic_filter,
    )

    baseline = [
        {"id": 1, "title": "洛天依「无限共鸣·纯蓝幻乐」巡回演唱会 - 虚拟偶像"},
        {"id": 2, "title": "杭州 · 許嵩2026《安泊猜想》巡迴演唱會"},
        {"id": 3, "title": "陆虎「像你这样的朋友3.0」巡演 - 流行挚友"},
    ]
    result = nodes.activity_research({
        "constraints": {
            "weekend_start": "2026-07-25T00:00:00+00:00",
            "weekend_end": "2026-07-27T00:00:00+00:00",
            "experience_requirements": ["继续寻找符合上一轮体验的不同选项"],
            "acceptance_criteria": ["符合原始体验要求"],
        },
        "candidate_cities": [{"city_code": "330100", "name": "杭州"}],
        "activities": baseline,
        "research_baseline_activities": baseline,
        "research_active_feedback": "不喜欢，再找找推荐其他演唱会",
        "follow_up_queries": ["杭州 其他演唱会"],
        "shown_activity_ids": [1, 2, 3],
    })

    assert [activity["id"] for activity in result["activities"]] == [105, 107]
    assert result["research_outcome"] == "improved"


def test_cross_source_title_normalization_handles_city_year_tags_and_traditional_text():
    from wheretogo.intel.dedup import same_event_title

    assert same_event_title(
        "洛天依「无限共鸣·纯蓝幻乐」巡回演唱会 - 虚拟偶像",
        "杭州·2026洛天依「无限共鸣纯蓝幻乐」巡回演唱会杭州站",
    )
    assert same_event_title(
        "陆虎「像你这样的朋友3.0」巡演 - 流行挚友",
        "杭州 · 陸虎2026《像你這樣的朋友3.0》巡演",
    )
    assert same_event_title(
        "【杭州】【官方特惠｜可订当日】宋城千古情（含景区+演出）",
        "【暑期特惠，需预约】杭州宋城千古情（凭身份证入场）",
    )
    assert not same_event_title("于贞「邀请函」巡演", "杭州摇滚新声音乐会")


def test_feedback_round_excludes_entities_shown_before_the_latest_baseline(monkeypatch):
    """第三轮也必须排除首轮实体，不能只比较最近一轮 baseline。"""
    import wheretogo.orchestration.nodes as nodes

    def _candidate(activity_id, title):
        return SimpleNamespace(
            id=activity_id,
            title=title,
            venue="浙江胜利剧院",
            category="演唱会",
            price_text=None,
            booking_url=None,
            start_at=datetime(2026, 8, 1, 10, tzinfo=timezone.utc),
            end_at=datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
            verification_status="public_source_observed",
            rerank_score=1.0,
            location=None,
            evidence={"source_type": "search"},
        )

    @contextmanager
    def _session():
        yield object()

    class _Retrieval:
        def retrieve_activities(self, *args, **kwargs):
            return [
                _candidate(
                    201,
                    "杭州 · 2026《漂洋過海來看你》經典演唱會2.0 | 浙江勝利劇院",
                ),
                _candidate(202, "杭州摇滚新声音乐会"),
            ]

    monkeypatch.setattr(nodes, "get_session", _session)
    monkeypatch.setattr(nodes, "_city_name", lambda *a: "杭州")
    monkeypatch.setattr(nodes, "_RETRIEVAL", _Retrieval())
    monkeypatch.setattr(nodes._deep_research, "should_run", lambda: False)

    result = nodes.activity_research({
        "constraints": {
            "weekend_start": "2026-08-01T00:00:00+00:00",
            "weekend_end": "2026-08-03T00:00:00+00:00",
        },
        "candidate_cities": [{"city_code": "330100", "name": "杭州"}],
        "activities": [{"id": 99, "title": "民谣之夜live现场演唱会"}],
        "research_baseline_activities": [{"id": 99, "title": "民谣之夜live现场演唱会"}],
        "research_active_feedback": "继续推荐其他演唱会",
        "follow_up_queries": ["杭州 其他演唱会"],
        "shown_activity_ids": [9, 99],
        "shown_activity_titles": [
            "杭州 · 2026《漂洋過海來看你》經典演唱會2.0一人一首成名曲全新演繹",
            "民谣之夜live现场演唱会",
        ],
    })

    assert [activity["id"] for activity in result["activities"]] == [202]


def test_exhausted_feedback_search_marks_retained_plan_instead_of_fake_new_plan():
    baseline = [{"id": i, "title": f"首轮活动{i}"} for i in range(1, 6)]
    reflected = activity_reflection({
        "activities": baseline,
        "research_active_feedback": "再找其他演唱会",
        "research_baseline_activities": baseline,
        "research_round_candidates": [],
        "research_improved": False,
        "research_loop_count": 3,
        "shown_activity_ids": [1, 2, 3, 4, 5],
        "constraints": {"interests": ["演唱会"]},
        "candidate_cities": [{"name": "杭州", "city_code": "330100"}],
    })
    assert reflected["activities"] == baseline
    assert reflected["research_outcome"] == "no_better_alternatives"
    assert reflected["research_should_continue"] is False

    bundle = compose_explore_bundle({
        "activities": reflected["activities"],
        "research_outcome": reflected["research_outcome"],
    })
    assert bundle["research_outcome"] == "no_better_alternatives"
    html = Path("web/index.html").read_text(encoding="utf-8")
    assert "本轮未找到不重复且更贴近反馈的可信活动" in html
    assert "上一轮保留方案" in html


def test_screenshot_regression_empty_round_has_zero_bundle_and_visible_empty_state():
    """截图回归：节点本轮 [] 时，bundle 不能继续声称有旧 5 项。"""
    state = {
        "plan_id": "391",
        "constraints": {"interests": ["展览"]},
        "candidate_cities": [{"city_code": "330100", "name": "杭州"}],
        "activities": [],
        "warnings": [
            "活动检索为空：可粘贴官方活动链接",
            "活动检索为空：可粘贴官方活动链接",
        ],
        "transport_options": {},
    }
    bundle = compose_explore_bundle(state)
    assert bundle["activities"] == []
    assert bundle["cities"][0]["driven_by_activities"]["value"] == 0
    assert bundle["warnings"] == ["活动检索为空：可粘贴官方活动链接"]
    html = Path("web/index.html").read_text(encoding="utf-8")
    assert 'data-empty="activities"' in html
    assert 'hasOwnProperty.call(d, "activities")' in html


def test_successful_later_round_removes_stale_empty_warning_from_bundle():
    bundle = compose_explore_bundle({
        "activities": [{"id": 9, "title": "新找到的活动"}],
        "candidate_cities": [{"city_code": "330100", "name": "杭州"}],
        "warnings": ["活动检索为空：可粘贴官方活动链接", "未提供出发地，按同城处理"],
    })
    assert bundle["warnings"] == ["未提供出发地，按同城处理"]


def test_extractor_receives_explicit_weekend(monkeypatch):
    captured = {}

    def _chat(route, messages, timeout):
        captured["body"] = messages[-1]["content"]
        return "[]"

    monkeypatch.setattr("wheretogo.intel.extract.chat", _chat)
    assert extract_activities("正文" * 50, "330100", "https://example.test", _wk()) == []
    assert "2026-07-25" in captured["body"]
    assert "2026-07-27" in captured["body"]
    assert "长展" in captured["body"]


def test_official_classifier_ignores_misleading_path_and_query():
    assert is_official_like("https://example.com/go?next=https://museum.gov.cn") is False
    assert is_official_like("https://culture.gov.cn/events/1") is True


def test_bff_uses_persisted_thread_and_prepares_feedback_before_research(monkeypatch):
    """BFF 集成：版本 thread_id 不得被 Planner 的 plan:id 默认值覆盖。"""
    client = TestClient(app)
    plan_id = client.post("/plans", json={"constraints": {
        "origins": ["上海"],
        "interests": ["展览"],
        "target_city_code": "330100",
        "weekend_start": "2026-07-25T00:00:00+08:00",
        "weekend_end": "2026-07-27T00:00:00+08:00",
    }}).json()["plan_id"]
    persisted_thread = f"plan:{plan_id}:acceptance-v2"
    with get_session() as session:
        plan = session.get(Plan, int(plan_id))
        plan.thread_id = persisted_thread
        session.commit()

    captured = {}

    class _Planner:
        def get_state(self, pid, *, thread_id=None):
            captured["state"] = (pid, thread_id)
            return SimpleNamespace(values={"constraints": {"interests": ["展览"]}})

        def stream_start(self, pid, constraints, *, thread_id=None):
            captured["stream"] = (pid, thread_id)
            return iter(())

        def prepare_research_more(self, pid, feedback, *, constraints=None, thread_id=None):
            captured["feedback"] = (pid, feedback, constraints, thread_id)

    fake = _Planner()
    bff_module = importlib.import_module("wheretogo.bff.app")
    monkeypatch.setattr(bff_module, "get_planner", lambda: fake)
    assert client.get(f"/plans/{plan_id}/stream").status_code == 200
    assert captured["stream"] == (plan_id, persisted_thread)

    response = client.post(
        f"/plans/{plan_id}/chat", json={"message": "这几个不喜欢，再找一批别的"}
    )
    assert response.status_code == 200
    assert response.json()["auto_stream"] is True
    assert captured["feedback"][0] == plan_id
    assert captured["feedback"][3] == persisted_thread


def test_bff_couple_followup_updates_same_checkpoint_and_never_restarts(monkeypatch):
    """截图三轮回归：软偏好应进入原 checkpoint 的 research-more，而非新线程旧缓存。"""
    client = TestClient(app)
    plan_id = client.post("/plans", json={"constraints": {
        "origins": ["上海"],
        "party_size": 1,
        "interests": ["博物馆"],
        "target_city_code": "330100",
        "weekend_start": "2026-08-01T00:00:00+08:00",
        "weekend_end": "2026-08-02T23:59:59+08:00",
    }}).json()["plan_id"]
    with get_session() as session:
        plan = session.get(Plan, int(plan_id))
        original_thread = plan.thread_id

    captured = {}

    class _Planner:
        def get_state(self, pid, *, thread_id=None):
            return SimpleNamespace(values={
                "constraints": {"interests": ["博物馆"]},
                "activities": [{"id": 1, "title": "上一轮博物馆"}],
            })

        def prepare_research_more(
            self, pid, feedback, *, constraints=None, thread_id=None
        ):
            captured.update({
                "pid": pid,
                "feedback": feedback,
                "constraints": constraints,
                "thread_id": thread_id,
            })

    bff_module = importlib.import_module("wheretogo.bff.app")
    monkeypatch.setattr(bff_module, "get_planner", lambda: _Planner())
    response = client.post(
        f"/plans/{plan_id}/chat",
        json={"message": "有没有适合情侣的博物馆"},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["intent"] == "deep_research"
    assert body["auto_stream"] is True
    assert not body.get("restart_stream")
    assert body["constraints"]["party_size"] == 2
    assert body["constraints"]["research_goal"] == "有没有适合情侣的博物馆"
    assert body["constraints"]["acceptance_criteria"] == ["有没有适合情侣的博物馆"]
    assert captured["feedback"] == "有没有适合情侣的博物馆"
    assert captured["constraints"]["research_goal"] == "有没有适合情侣的博物馆"
    assert captured["thread_id"] == original_thread
    with get_session() as session:
        assert session.get(Plan, int(plan_id)).thread_id == original_thread


def test_prepare_research_more_builds_a_new_preference_aware_query():
    planner = PlannerService.__new__(PlannerService)
    planner.graph = MagicMock()
    planner.prepare_research_more(
        "948",
        "有没有适合情侣的博物馆",
        constraints={
            "interests": ["博物馆"],
            "soft_preferences": ["情侣约会"],
            "query": "周末博物馆 忌讳无",
        },
        thread_id="plan:948:existing",
        conversation=[
            {"role": "user", "content": "有没有适合情侣的博物馆"},
            {"role": "assistant", "content": "我会继续核实。"},
        ],
    )
    config, values = planner.graph.update_state.call_args.args[:2]
    assert config["configurable"]["thread_id"] == "plan:948:existing"
    assert values["research_feedback"] == "有没有适合情侣的博物馆"
    assert values["constraints"]["soft_preferences"] == ["情侣约会"]
    assert "偏好情侣约会" in values["constraints"]["query"]
    assert "有没有适合情侣的博物馆" in values["constraints"]["query"]
    assert values["constraints"]["query"] != "周末博物馆 忌讳无"
    assert values["research_personalized"] is False
    assert values["conversation"][-1]["content"] == "我会继续核实。"


def test_open_preference_reranks_and_explains_candidates(monkeypatch):
    original = [
        {
            "id": 1,
            "title": "宋韵千年——百馆联动展",
            "category": "博物馆",
            "venue": "城市博物馆",
            "rerank_score": 0.9,
        },
        {
            "id": 2,
            "title": "灰色博物馆——让情绪被看见",
            "category": "沉浸式心灵治愈展",
            "venue": "艺术中心",
            "rerank_score": 0.1,
        },
    ]
    monkeypatch.setattr(
        "wheretogo.research.semantics.evaluate_candidates",
        lambda candidates, **_kwargs: (
            [
                {
                    **candidates[1],
                    "preference_match": ["共同体验有来源支持"],
                    "preference_match_basis": "按原始需求与来源证据进行语义评审",
                },
                candidates[0],
            ],
            {
                "evaluated": True,
                "matched_count": 1,
                "criterion_coverage": 1.0,
            },
        ),
    )
    ranked, personalized = _personalize_activities(
        original, ["情侣约会"], "有没有适合情侣的博物馆"
    )
    assert personalized is True
    assert [item["id"] for item in ranked] == [2, 1]
    assert ranked[0]["preference_match"] == ["共同体验有来源支持"]
    assert ranked[0]["preference_match_basis"].startswith("按原始需求")
    assert "preference_match" not in original[1]


def test_reflection_reports_reranked_instead_of_claiming_identical_results_are_new():
    baseline = [
        {"id": 1, "title": "普通博物馆"},
        {
            "id": 2,
            "title": "沉浸治愈艺术展",
            "preference_match": ["情侣约会·沉浸互动"],
        },
    ]
    reflected = activity_reflection({
        "activities": list(reversed(baseline)),
        "research_feedback": None,
        "research_active_feedback": "有没有适合情侣的博物馆",
        "research_loop_count": 1,
        "research_improved": False,
        "research_personalized": True,
        "research_baseline_activities": baseline,
        "research_round_candidates": [],
        "shown_activity_ids": [1, 2],
        "shown_activity_titles": ["普通博物馆", "沉浸治愈艺术展"],
        "constraints": {
            "interests": ["博物馆"],
            "soft_preferences": ["情侣约会"],
        },
        "candidate_cities": [{"name": "杭州"}],
    })
    assert reflected["research_outcome"] == "reranked"
    assert reflected["activities"][0]["id"] == 2


def test_frontend_distinguishes_reranked_from_new_results():
    html = Path("web/index.html").read_text(encoding="utf-8")
    assert 'researchOutcome === "reranked"' in html
    assert 'data-preference-match="true"' in html
    assert "本轮未找到新的可信活动；已按你的新增偏好重新排序" in html


def test_research_more_can_resume_after_feedback_moves_to_active_state(monkeypatch):
    """进程/网络中断后 feedback 已消费但 active_feedback 尚在，续流不能错误 409。"""
    client = TestClient(app)
    plan_id = client.post("/plans", json={"constraints": {
        "origins": ["上海"],
        "interests": ["博物馆"],
    }}).json()["plan_id"]

    class _Planner:
        def get_state(self, pid, *, thread_id=None):
            return SimpleNamespace(values={
                "research_feedback": None,
                "research_active_feedback": "有没有适合情侣的博物馆",
            })

        def stream_research_more(self, pid, *, thread_id=None):
            return iter(())

    bff_module = importlib.import_module("wheretogo.bff.app")
    monkeypatch.setattr(bff_module, "get_planner", lambda: _Planner())
    response = client.post(f"/plans/{plan_id}/research-more")
    assert response.status_code == 200


def test_bff_refine_field_requests_full_stream_restart(monkeypatch):
    """偏好字段变化后必须重跑完整规划，不能被首轮 autoPlanned 状态拦截。"""
    client = TestClient(app)
    plan_id = client.post("/plans", json={"constraints": {
        "origins": ["上海"],
        "interests": ["演唱会"],
        "target_city_code": "330100",
        "weekend_start": "2026-08-01T00:00:00+08:00",
        "weekend_end": "2026-08-02T23:59:59+08:00",
    }}).json()["plan_id"]
    with get_session() as session:
        old_thread = session.get(Plan, int(plan_id)).thread_id

    bff_module = importlib.import_module("wheretogo.bff.app")
    monkeypatch.setattr(
        bff_module,
        "handle_turn",
        lambda *args, **kwargs: {
            "plan_id": plan_id,
            "intent": "refine_field",
            "action": "update_state",
            "reply": "好的，已更新：想玩展览。",
            "constraints_patch": {"interests": ["展览"]},
            "booking": None,
            "pending_clarify": [],
        },
    )

    response = client.post(
        f"/plans/{plan_id}/chat",
        json={"message": "我不想看演唱会了 我想逛展"},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["ready_to_plan"] is True
    assert body["restart_stream"] is True
    assert body.get("auto_stream") is not True
    assert body["constraints"]["interests"] == ["展览"]
    with get_session() as session:
        assert session.get(Plan, int(plan_id)).thread_id != old_thread
