"""调度（DD-06 §5.1/§5.2/§5.9）：到期源抓取 + 过期清理。

v0.1 用普通同步函数作为"任务体"（Celery eager 语义）；生产可包 @celery_app.task 装饰，接口不变。
无需 broker 即可单测。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db.session import SessionLocal, get_session
from ..models import SourceRegistry
from .ingest import expire_activities, process_source


def due_sources(session: Session, limit: int = 200) -> list[int]:
    """到期（且 enabled+robots_ok）的 source_registry id。"""
    return list(session.execute(
        text(
            "SELECT id FROM source_registry "
            "WHERE enabled = TRUE AND robots_ok = TRUE "
            "AND (last_fetched_at IS NULL OR last_fetched_at + fetch_interval < now()) "
            "ORDER BY last_fetched_at NULLS FIRST LIMIT :lim"
        ),
        {"lim": limit},
    ).scalars())


def crawl_source(source_id: int, session: Session | None = None, allow_fetch=None) -> dict:
    """单源全管线（调度器与手动重跑复用）。"""
    own = session is None
    s = session or SessionLocal()
    try:
        src = s.get(SourceRegistry, source_id)
        if not src or not src.enabled:
            return {"source_id": source_id, "ingested": []}
        ids = process_source(src, s, allow_fetch=allow_fetch)
        if own:
            s.commit()
        return {"source_id": source_id, "ingested": ids}
    finally:
        if own:
            s.close()


def dispatch_due_sources(allow_fetch=None) -> dict:
    """单轮调度：抓取所有到期源。"""
    with get_session() as s:
        ids = due_sources(s)
    total = 0
    for sid in ids:
        total += len(crawl_source(sid, allow_fetch=allow_fetch).get("ingested", []))
    return {"crawled": len(ids), "ingested_total": total}


def expire_activities_task() -> dict:
    """beat 周期任务（DD-06 §5.9）：过期下架。"""
    return {"expired": expire_activities()}
