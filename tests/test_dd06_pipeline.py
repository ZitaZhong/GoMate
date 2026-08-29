"""DD-06 活动情报流水线验收（对应 DD-06 §11.3 DoD）。离线：注入 fetch + mock 抽取。"""
from __future__ import annotations

from datetime import datetime, timezone

from wheretogo.enums import SourceType, VerificationStatus
from wheretogo.intel import (
    ActivityDraft,
    ingest_realtime,
    make_fingerprint,
    process_source,
)
from wheretogo.intel.dedup import find_duplicate
from wheretogo.intel.extract import FieldQuote
from wheretogo.intel.grade import grade_activity
from wheretogo.intel.ingest import expire_activities
from wheretogo.intel.normalize import NormalizeError, normalize_activity
from wheretogo.models import Activity, ActivityReviewQueue, SourceRegistry

# 抽取草稿的活动时间（须为未来日期：normalize 拦截已过期活动）
_START_TEXT = "2026年12月26日 10:00"

_HTML = (
    "<html><body><article>"
    f"古埃及文明大展 展览时间 {_START_TEXT} 票价 ¥100 购票 https://mus.example/buy"
    "</article></body></html>"
)


class _Resp:
    def __init__(self, status: int, body: str = "", etag: str | None = None) -> None:
        self.status_code = status
        self.text = body
        self.headers = {"etag": etag} if etag else {}


class _Src:
    def __init__(self, **kw) -> None:
        self.entry_url = "https://mus.example/x"
        self.robots_ok = True
        self.source_type = SourceType.official_venue
        self.city_code = "310000"
        self.id = 1
        self.last_fetched_at = None
        self.__dict__.update(kw)


def _draft(url: str = "https://mus.example/x", *, grounded: bool = True) -> ActivityDraft:
    """一条抽取草稿。grounded=False 时引用不在正文中（触发 quote_mismatch）。"""
    start_q = _START_TEXT if grounded else "完全不在正文里的时间XXX"
    price_q = "票价 ¥100" if grounded else "瞎编的价格YYY"
    booking_q = "购票 https://mus.example/buy" if grounded else "瞎编的链接ZZZ"
    return ActivityDraft(
        title="古埃及文明大展", venue=None,
        start_text=FieldQuote(value=_START_TEXT, evidence_quote=start_q),
        price=FieldQuote(value="¥100", evidence_quote=price_q),
        booking=FieldQuote(value="https://mus.example/buy", evidence_quote=booking_q),
        category="展览", city_code="310000", source_url=url,
    )


def _fetch_ok(url, etag=None, timeout=15):
    return _Resp(200, _HTML, "e1")


# —— 指纹去重 ——
def test_fingerprint_stable():
    class N:
        title = "古埃及展"
        city_code = "310000"
        venue = "上博东馆"
        start_at = datetime(2026, 7, 25, tzinfo=timezone.utc)

    assert make_fingerprint(N()) == make_fingerprint(N())
    assert len(make_fingerprint(N())) == 40


def test_fingerprint_differ_by_title():
    class N:
        def __init__(self, t):
            self.title = t
            self.city_code = "310000"
            self.venue = None
        start_at = datetime(2026, 7, 25, tzinfo=timezone.utc)

    assert make_fingerprint(N("A")) != make_fingerprint(N("B"))


# —— quote 回锚 ——
def test_quotes_grounded_in():
    assert _draft().quotes_grounded_in(_HTML)
    bad = ActivityDraft(
        title="x",
        start_text=FieldQuote(value="x", evidence_quote="不存在的引用"),
        price=FieldQuote(value="y"),
    )
    assert not bad.quotes_grounded_in(f"展览时间 {_START_TEXT} 票价 ¥100")


def test_normalize_rejects_empty_title_and_implausible_multiyear_span():
    empty = _draft()
    empty.title = "  "
    try:
        normalize_activity(empty, _Src())
        raise AssertionError("empty title must be rejected")
    except NormalizeError as exc:
        assert "title missing" in str(exc)

    too_long = _draft()
    too_long.start_text = FieldQuote(value="2023年8月29日 10:00")
    too_long.end_text = FieldQuote(value="2026年8月29日 18:00")
    try:
        normalize_activity(too_long, _Src())
        raise AssertionError("multi-year activity span must be rejected")
    except NormalizeError as exc:
        assert "span exceeds" in str(exc)

    wrong_year = _draft()
    wrong_year.title = "2025梦想天堂演唱会"
    wrong_year.start_text = FieldQuote(value=_START_TEXT)
    try:
        normalize_activity(wrong_year, _Src())
        raise AssertionError("title/date year conflict must be rejected")
    except NormalizeError as exc:
        assert "title year" in str(exc)

    past = _draft()
    past.start_text = FieldQuote(value="2017年5月20日 10:00")
    try:
        normalize_activity(past, _Src())
        raise AssertionError("past activity must be rejected")
    except NormalizeError as exc:
        assert "past date" in str(exc)


# —— 定级 ——
def test_grade_levels():
    class N:
        start_at = datetime(2026, 7, 25, 10, tzinfo=timezone.utc)
        end_at = None
        price_text = "¥100"
        booking_url = "https://x"
        venue = None
        category = "展览"
        title = "展"
        city_code = "310000"
        source_url = "https://s.example"

        def embed_text(self):
            return self.title

    class Src:
        def __init__(self, st):
            self.source_type = st
            self.city_code = "310000"

    assert grade_activity(N(), Src(SourceType.official_venue))["verification_status"] == "official_source_confirmed"
    # DD-03 §4：search/community 仅作入口、未核实 → unknown（不进可信召回，待 Phase 3 核实升级）
    assert grade_activity(N(), Src(SourceType.search))["verification_status"] == "unknown"
    assert grade_activity(N(), Src(SourceType.community))["verification_status"] == "unknown"
    assert grade_activity(N(), Src(SourceType.llm))["verification_status"] == "estimated"


# —— 全管线入库（mock fetch + 抽取）——
def test_process_source_ingests_activity(session, monkeypatch):
    src = _Src()
    monkeypatch.setattr("wheretogo.intel.fetcher._robots_ok", lambda url: True)
    monkeypatch.setattr("wheretogo.intel.ingest.extract_activities",
                        lambda clean_md, city, url: [_draft(url)])
    ids = process_source(src, session, allow_fetch=_fetch_ok)
    assert len(ids) == 1
    a = session.get(Activity, ids[0])
    assert a.title == "古埃及文明大展"
    assert a.verification_status == VerificationStatus.official_source_confirmed
    assert a.embedding is not None and a.fingerprint is not None


# —— quote_mismatch → 审核队列 ——
def test_quote_mismatch_enqueues_review(session, monkeypatch):
    src = _Src()
    monkeypatch.setattr("wheretogo.intel.fetcher._robots_ok", lambda url: True)
    monkeypatch.setattr("wheretogo.intel.ingest.extract_activities",
                        lambda clean_md, city, url: [_draft(url, grounded=False)])
    base = session.query(ActivityReviewQueue).filter_by(reason="quote_mismatch").count()
    ids = process_source(src, session, allow_fetch=lambda url, etag=None, timeout=15: _Resp(200, _HTML))
    assert ids == []
    # 断言本用例新增 1 条（不假设表为空，对共享开发库已有数据鲁棒）
    assert session.query(ActivityReviewQueue).filter_by(reason="quote_mismatch").count() == base + 1


# —— robots 拒抓 ——
def test_robots_disallow_skips(session, monkeypatch):
    src = _Src()
    monkeypatch.setattr("wheretogo.intel.fetcher._robots_ok", lambda url: False)
    called = {"n": 0}

    def fake_fetch(url, etag=None, timeout=15):
        called["n"] += 1
        return _Resp(200, _HTML)

    ids = process_source(src, session, allow_fetch=fake_fetch)
    assert ids == [] and called["n"] == 0


# —— ETag 304 → 不重抽 ——
def test_etag_304_skips_extract(session, monkeypatch):
    src = _Src()
    monkeypatch.setattr("wheretogo.intel.fetcher._robots_ok", lambda url: True)
    ids = process_source(src, session, allow_fetch=lambda url, etag=None, timeout=15: _Resp(304))
    assert ids == []


# —— ingest_realtime 同步入库 ——
def test_ingest_realtime_returns_ids(session, monkeypatch):
    monkeypatch.setattr("wheretogo.intel.fetcher._robots_ok", lambda url: True)
    monkeypatch.setattr("wheretogo.intel.ingest.extract_activities",
                        lambda clean_md, city, url: [_draft(url)])
    ids = ingest_realtime(["https://mus.example/x"], "310000", session=session,
                          source_type=SourceType.official_venue, allow_fetch=_fetch_ok)
    assert len(ids) == 1
    src = session.query(SourceRegistry).filter_by(entry_url="https://mus.example/x").one()
    assert src.source_type == SourceType.official_venue


# —— 过期下架 ——
def test_expire_activities(session, make_activity):
    a = make_activity("即将过期")
    a.expires_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    session.flush()
    n = expire_activities(session)
    assert n >= 1
    session.refresh(a)
    assert a.verification_status == VerificationStatus.expired


# —— 语义去重（pgvector 二级）——
def test_semantic_dedup(session, make_activity):
    a = make_activity("莫奈特展", venue="中华艺术宫", category="展览", price_text="¥120")

    class N:
        title = "莫奈特展"
        city_code = "310000"
        venue = "中华艺术宫"
        category = "展览"
        price_text = "¥120"
        start_at = a.start_at
        end_at = None
        booking_url = None
        source_url = "x"
        fingerprint = "unique-not-matching-fingerprint"

        def embed_text(self):
            return "莫奈特展 中华艺术宫 展览 ¥120"

    dup = find_duplicate(N(), session)
    assert dup is not None and dup.matched in {"entity_title", "semantic"}


# —— 指纹精确去重 ——
def test_fingerprint_dedup(session, make_activity):
    a = make_activity("毕加索展", venue="美术馆", category="展览", price_text="¥80")

    class N:
        title = "毕加索展"
        city_code = "310000"
        venue = "美术馆"
        category = "展览"
        price_text = "¥80"
        start_at = a.start_at
        end_at = None
        booking_url = None
        source_url = "x"
        fingerprint = None

        def embed_text(self):
            return "x"

    n = N()
    n.fingerprint = make_fingerprint(n)
    a.fingerprint = n.fingerprint
    session.flush()
    dup = find_duplicate(n, session)
    assert dup is not None and dup.matched == "fingerprint"
