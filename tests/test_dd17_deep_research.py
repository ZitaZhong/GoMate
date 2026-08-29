"""DD-17 实时深度研究验收（对应 DD-17 §10 DoD）。离线：mock 搜索 + 入库。"""
from __future__ import annotations

from datetime import datetime, timezone

from wheretogo.enums import VerificationStatus
from wheretogo.models import DeepResearchJob
from wheretogo.research import (
    DeepResearchResult,
    build_brief,
    deep_research,
    needs_deep_research,
    split_subtopics,
)
from wheretogo.retrieval import Weekend


def _wk() -> Weekend:
    return Weekend(datetime(2026, 7, 25, tzinfo=timezone.utc), datetime(2026, 7, 27, tzinfo=timezone.utc))


# —— 触发判定 ——
def test_needs_deep_research_gating():
    from wheretogo.config import Settings

    on = Settings(deep_research_enabled=True)  # 显式开启（不依赖全局 env）
    assert needs_deep_research(settings=on) is True  # intent=None
    assert needs_deep_research("provide_constraints", settings=on) is True
    assert needs_deep_research("refine_field", settings=on) is True
    assert needs_deep_research("chitchat", settings=on) is False  # 纯闲聊不触发
    assert needs_deep_research("ask_info", settings=on) is False
    assert needs_deep_research(settings=Settings(deep_research_enabled=False)) is False


def test_needs_deep_research_disabled():
    from wheretogo.config import Settings
    assert needs_deep_research(settings=Settings(deep_research_enabled=False)) is False


# —— brief / 子主题 ——
def test_build_brief_and_split_subtopics(monkeypatch):
    monkeypatch.setattr(
        "wheretogo.research.semantics.extract_json",
        lambda *_args, **_kwargs: {
            "tasks": [{
                "query": "上海 莫奈展 官方",
                "tool": "web_search",
                "purpose": "核实用户目标",
            }]
        },
    )
    brief = build_brief(
        "310000",
        _wk(),
        None,
        None,
        research_goal="寻找莫奈展",
        acceptance_criteria=["有官方来源"],
    )
    assert brief["city_code"] == "310000"
    topics = split_subtopics(brief)
    assert topics == ["上海 莫奈展 官方"]


# —— 全管线（mock 开放域搜索 + 入库）——
def _mock_search(monkeypatch, results, ingest_ids=None):
    """mock supervisor 的开放域搜索(provider_call) + 入库(ingest_content)。"""
    from wheretogo.providers.base import Result

    # content 补足到 ≥80 字（触发 ingest_content 主路径，而非 fetch 回退）
    data = [{"url": u, "content": (c + "　" * max(0, 100 - len(c))), "title": "t"} for u, c in results]
    monkeypatch.setattr("wheretogo.research.supervisor.provider_call",
                        lambda name, op, params: Result(ok=True, data={"results": data}, source_type="search"))
    monkeypatch.setattr("wheretogo.research.supervisor.is_official_like", lambda u: True)
    if ingest_ids is not None:
        monkeypatch.setattr("wheretogo.research.supervisor.ingest_content",
                            lambda url, content, city_code, source_type=None, session=None,
                                   weekend=None, raise_on_error=False: list(ingest_ids))


def test_deep_research_ingests_and_records_job(session, monkeypatch):
    _mock_search(monkeypatch, [("https://museum.example/x", "古埃及展 本周末 10:00")], ingest_ids=[101, 102])
    res = deep_research("310000", _wk(), ["展览"], session=session)
    assert isinstance(res, DeepResearchResult)
    assert res.activity_ids == [101, 102]
    assert res.degraded is False
    assert res.job_id is not None
    job = session.get(DeepResearchJob, res.job_id)
    assert job.status == "succeeded"
    assert job.source_count >= 1


def test_deep_research_degraded_when_no_sources(session, monkeypatch):
    _mock_search(monkeypatch, [])
    res = deep_research("310000", _wk(), ["展览"], session=session)
    assert res.degraded is True
    assert res.activity_ids == []


# —— 防抖缓存 ——
def test_deep_research_cache_hit(session, monkeypatch):
    _mock_search(monkeypatch, [("https://museum.example/x", "展 本周末")], ingest_ids=[1])
    r1 = deep_research("310000", _wk(), ["展览"], session=session)
    assert r1.cache_hit is False
    r2 = deep_research("310000", _wk(), ["展览"], session=session)
    assert r2.cache_hit is True  # 相同 query 命中防抖缓存


# —— 护栏：深搜无源 → degraded，不直产 confirmed ——
def test_deep_research_no_fabricated_confirm(session, monkeypatch):
    _mock_search(monkeypatch, [])
    res = deep_research("310000", _wk(), ["展览"], nl_query="北京 高铁 票价", session=session)
    assert res.degraded is True
    assert res.activity_ids == []


# —— Phase 3 交叉验证升级（DD-03 §4：search 核实后才可升，只升不降）——
def test_phase3_upgrade_verified(session, make_activity):
    from wheretogo.research.supervisor import _upgrade_verified

    a = make_activity("未核实活动", verification_status=VerificationStatus.unknown)
    a.evidence = {"source_type": "search", "source_url": "https://entry.example/x",
                  "verification_status": "unknown", "confidence": 0.4,
                  "note": "activity.start_at 来自 search，仅作入口未核实"}
    session.flush()

    # 公开第二来源核实 → public_source_observed（进入可信召回），来源不变，note 记录核实路径
    _upgrade_verified(session, a, "https://blog.example/y", official=False)
    assert a.verification_status == VerificationStatus.public_source_observed
    assert a.evidence["verification_status"] == "public_source_observed"
    assert a.evidence["source_type"] == "search"
    assert "交叉核实" in a.evidence["note"] and "https://blog.example/y" in a.evidence["note"]

    # 官方白名单第二来源 → official_source_confirmed（来源切换为官方源，否则过不了 Guard 来源校验）
    _upgrade_verified(session, a, "https://museum.gov.cn/z", official=True)
    assert a.verification_status == VerificationStatus.official_source_confirmed
    assert a.evidence["source_type"] == "official_venue"
    assert a.evidence["source_url"] == "https://museum.gov.cn/z"

    # 只升不降：再用公开源核实不回退
    _upgrade_verified(session, a, "https://blog.example/w", official=False)
    assert a.verification_status == VerificationStatus.official_source_confirmed
