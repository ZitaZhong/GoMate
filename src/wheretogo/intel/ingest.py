"""入库（DD-06 §5.8/§5.9 + v2 ingest_realtime）：`activities` 唯一写入方。

铁律：规划流（DD-02/05）只读 activities；本模块经 fetch/clean/extract/normalize/dedup/grade/embed
全管线写入。fingerprint UNIQUE 保证幂等；去重/冲突/低置信入 activity_review_queue。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from geoalchemy2 import WKTElement
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..db.session import SessionLocal
from ..enums import SourceType, VerificationStatus
from ..models import Activity, ActivityReviewQueue, RawPage, SourceRegistry
from ..retrieval import RetrievalService
from .clean import clean_to_markdown
from .dedup import TRUST, find_duplicate
from .extract import extract_activities
from .fetcher import fetch_page
from .grade import grade_activity
from .normalize import NormalizeError, normalize_activity

DEFAULT_TTL = timedelta(days=14)
_svc: RetrievalService | None = None
logger = logging.getLogger(__name__)


def _get_svc() -> RetrievalService:
    global _svc
    if _svc is None:
        _svc = RetrievalService()
    return _svc


def _to_geo(loc: tuple[float, float] | None):
    if not loc:
        return None
    return WKTElement(f"POINT({loc[0]} {loc[1]})", srid=4326)


def _src_st(src) -> str:
    st = src.source_type
    return st.value if hasattr(st, "value") else str(st)


def upsert_activity(n, graded: dict, session: Session, embedding=None,
                    embedding_version: str | None = None, supersede: int | None = None) -> int | None:
    """写 activities（ON CONFLICT fingerprint 幂等）；返回 activity id。倒序时间不写（N1 安全网）。"""
    if n.start_at is not None and n.end_at is not None and n.end_at < n.start_at:
        return None  # 结束早于开始（正常已在 normalize 拦截），不入库
    svc = _get_svc()
    emb = embedding if embedding is not None else svc.embed([n.embed_text()])[0]
    # 版本按实际产出 provider 如实标注：哈希兜底写 'hashing-ngram-v1'，不冒名 API 模型版本
    ev = embedding_version or svc.embedding_version()
    expires_at = n.end_at or (n.start_at + DEFAULT_TTL if n.start_at else None)
    values = {
        "fingerprint": n.fingerprint, "title": n.title, "city_code": n.city_code,
        "venue": n.venue, "location": _to_geo(n.location),
        "start_at": n.start_at, "end_at": n.end_at,
        "price_text": n.price_text, "booking_url": n.booking_url, "category": n.category,
        "evidence": graded["evidence"], "verification_status": graded["verification_status"],
        "availability_status": "user_must_confirm",
        "embedding": emb, "embedding_version": ev, "expires_at": expires_at,
    }
    stmt = pg_insert(Activity).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["fingerprint"],
        set_={k: values[k] for k in (
            "evidence", "verification_status", "price_text", "booking_url", "end_at",
            "embedding", "embedding_version", "expires_at")},
    )
    session.execute(stmt)
    act = session.query(Activity).filter_by(fingerprint=n.fingerprint).one_or_none()
    act_id = act.id if act else None
    if supersede and act_id:
        session.query(Activity).filter_by(id=supersede).update(
            {"verification_status": VerificationStatus.expired.value,
             "expires_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)},
            synchronize_session=False,
        )
    return act_id


def expire_activities(session: Session | None = None) -> int:
    """过期下架：expires_at < now → verification_status=expired。"""
    own = session is None
    s = session or SessionLocal()
    try:
        r = s.execute(text(
            "UPDATE activities SET verification_status='expired', updated_at=now() "
            "WHERE expires_at < now() AND verification_status != 'expired'"
        ))
        n = r.rowcount or 0
        if own:
            s.commit()
        return n
    finally:
        if own:
            s.close()


def enqueue_review(session: Session, *, raw_page_id: int | None = None, source_id: int | None = None,
                   reason: str, draft: dict | None = None, conflict_with: int | None = None) -> None:
    session.add(ActivityReviewQueue(raw_page_id=raw_page_id, source_id=source_id, reason=reason,
                                   draft=draft, conflict_with=conflict_with))
    session.flush()


def ensure_source(url: str, city_code: str, source_type, session: Session) -> SourceRegistry:
    src = session.query(SourceRegistry).filter_by(entry_url=url).one_or_none()
    if src:
        return src
    st_val = source_type.value if hasattr(source_type, "value") else source_type
    src = SourceRegistry(
        name=url[:120], city_code=city_code, source_type=source_type, entry_url=url,
        parser_kind="static", robots_ok=True, trust_level=TRUST.get(st_val, 9), enabled=True,
    )
    session.add(src)
    session.flush()
    return src


def process_source(src, session: Session, weekend=None, allow_fetch=None) -> list[int]:
    """单源全管线：fetch→clean→extract→normalize→dedup→grade→upsert。返回入库 activity ids。"""
    fr = fetch_page(src, allow_fetch=allow_fetch)
    if not fr.robots_allowed or fr.error:
        return []
    rp = RawPage(source_id=src.id, url=fr.url, http_status=fr.http_status,
                 content_hash=fr.content_hash, etag=fr.etag, clean_md=None)
    session.add(rp)
    session.flush()
    if fr.from_cache or not fr.html:
        return []  # 304 或无正文 → 无可抽取

    clean_md = clean_to_markdown(fr.html, fr.url)
    # JS 渲染兜底：httpx 清洗后正文过薄（疑似 SPA）→ Playwright 重渲染再清洗（效果优先）
    if len(clean_md) < 500 and allow_fetch is None:
        from .fetcher import _playwright_get
        try:
            _, pw_html = _playwright_get(fr.url)
            pw_md = clean_to_markdown(pw_html, fr.url)
            if len(pw_md) > len(clean_md):
                clean_md = pw_md
        except Exception:
            pass  # Playwright 不可用/失败 → 用 httpx 结果继续
    rp.clean_md = clean_md
    ids = _ingest_drafts_from_md(clean_md, src, rp.id, session, weekend=weekend)
    src.last_fetched_at = datetime.now(timezone.utc)
    return ids


def _ingest_drafts_from_md(
    clean_md: str, src, rp_id: int, session: Session, weekend=None
) -> list[int]:
    """共享抽取→入库循环：quote 回锚 → normalize → dedup → grade → upsert。返回 activity ids。"""
    # 保持旧扩展/测试桩的三参数兼容；真实研究提供窗口时走新契约。
    drafts = (
        extract_activities(clean_md, src.city_code, src.entry_url, weekend=weekend)
        if weekend is not None
        else extract_activities(clean_md, src.city_code, src.entry_url)
    )
    ids: list[int] = []
    my_trust = TRUST.get(_src_st(src), 9)
    for d in drafts:
        if not d.quotes_grounded_in(clean_md):
            enqueue_review(session, raw_page_id=rp_id, source_id=src.id,
                           reason="quote_mismatch", draft=d.model_dump())
            continue
        try:
            n = normalize_activity(d, src)
        except NormalizeError:
            enqueue_review(session, raw_page_id=rp_id, source_id=src.id,
                           reason="normalize_failed", draft=d.model_dump())
            continue
        dup = find_duplicate(n, session)
        graded = grade_activity(n, src, fetched_at=datetime.now(timezone.utc))
        if dup:
            # 多源交叉验证：对比新旧日期
            existing = session.query(Activity).filter_by(id=dup.id).one_or_none()
            if existing and existing.start_at and n.start_at:
                if existing.start_at.date() != n.start_at.date():
                    # 日期冲突 → 标注争议，降低置信度
                    note = f"日期有争议：来源1={existing.start_at.date()} vs 来源2={n.start_at.date()}"
                    graded["evidence"]["note"] = note
                    graded["evidence"]["confidence"] = min(0.4, graded["evidence"].get("confidence", 0.5))
                    # 更新已有记录的evidence标注争议
                    old_ev = dict(existing.evidence or {})
                    old_ev["note"] = note
                    old_ev["confidence"] = min(0.4, old_ev.get("confidence", 0.5))
                    session.query(Activity).filter_by(id=dup.id).update(
                        {"evidence": old_ev, "updated_at": datetime.now(timezone.utc)},
                        synchronize_session=False)
                else:
                    # 日期一致 → 多源确认，提升置信度
                    old_ev = dict(existing.evidence or {})
                    old_ev["confidence"] = min(0.9, old_ev.get("confidence", 0.6) + 0.1)
                    if not old_ev.get("note") or "争议" not in old_ev.get("note", ""):
                        old_ev["note"] = "多源确认日期一致"
                    session.query(Activity).filter_by(id=dup.id).update(
                        {"evidence": old_ev, "updated_at": datetime.now(timezone.utc)},
                        synchronize_session=False)
            if my_trust < dup.trust_level:  # 本源更可信 → 覆盖
                # fingerprint 命中时 upsert 更新的就是同一行，绝不能再把自己标 expired。
                supersede = dup.id if dup.matched == "semantic" else None
                aid = upsert_activity(n, graded, session, supersede=supersede)
                if aid:
                    ids.append(aid)
            else:
                enqueue_review(session, raw_page_id=rp_id, source_id=src.id, reason="duplicate",
                               draft=d.model_dump(), conflict_with=dup.id)
            continue
        aid = upsert_activity(n, graded, session)
        if aid:
            ids.append(aid)
    return ids


def ingest_content(url: str, content: str | None, city_code: str,
                   source_type=SourceType.search, session: Session | None = None,
                   weekend=None, raise_on_error: bool = False) -> list[int]:
    """开放域深搜主路径：从已抓取正文（Tavily raw_content 等）直接抽取入库，跳过 fetch/JS 渲染。"""
    if not content or len(content) < 80:
        return []
    own = session is None
    s = session or SessionLocal()
    try:
        src = ensure_source(url, city_code, source_type, s)
        rp = RawPage(source_id=src.id, url=url, http_status=200,
                     content_hash=None, etag=None, clean_md=content[:50000])
        s.add(rp)
        s.flush()
        ids = _ingest_drafts_from_md(
            content, src, rp.id, s, weekend=weekend
        )
        src.last_fetched_at = datetime.now(timezone.utc)
        if own:
            s.commit()
        return ids
    except Exception:
        if own:
            try:
                s.rollback()
            except Exception:
                pass
        logger.exception("ingest_content failed: url=%s city=%s", url, city_code)
        if raise_on_error:
            raise
        return []
    finally:
        if own:
            s.close()


def ingest_realtime(urls, city_code: str, weekend=None,
                    source_type=SourceType.search, session: Session | None = None,
                    allow_fetch=None) -> list[int]:
    """v2 同步入库入口（DD-17）：复用全管线，返回入库 activity ids。"""
    own = session is None
    s = session or SessionLocal()
    ids: list[int] = []
    try:
        for url in urls:
            src = ensure_source(url, city_code, source_type, s)
            ids += process_source(src, s, weekend=weekend, allow_fetch=allow_fetch)
        if own:
            s.commit()
    except Exception:
        if own:
            s.rollback()
        raise
    finally:
        if own:
            s.close()
    return ids


def ingest_user_url(url: str, city_code: str, session: Session | None = None,
                    allow_fetch=None) -> list[int]:
    """用户提供链接（PRD 来源第 5 类 user_provided）。"""
    return ingest_realtime([url], city_code, source_type=SourceType.user_provided,
                           session=session, allow_fetch=allow_fetch)
