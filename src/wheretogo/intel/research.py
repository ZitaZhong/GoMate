"""补搜（DD-06 §5.10）：只找官方入口 URL，事实回本模块管线定级。

v0.1 不依赖 gpt-researcher 库；用 DD-04 search Provider 找候选 URL，判为官方站后注册为
`official_venue` 源、走 process_source 抓取抽取定级。**搜索报告文本绝不入库为事实。**
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db.session import SessionLocal
from ..enums import SourceType
from ..providers import call as provider_call
from .ingest import ensure_source, process_source

_OFFICIAL_HINTS = (
    ".gov.cn", ".gov", "museum", "theatre", "theater", "culture", "artmuseum",
    "symphony", "opera", "official", "venue", "park", "center", "arts",
)


def is_official_like(url: str) -> bool:
    """仅检查规范化 hostname，避免 path/query 中的 “official/gov” 诱导误判。"""
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if not host:
        return False
    if host.endswith(".gov.cn") or host.endswith(".gov"):
        return True
    labels = set(filter(None, re.split(r"[.\-_]", host)))
    return any(h.lstrip(".") in labels for h in _OFFICIAL_HINTS if not h.startswith(".gov"))


def discover_official_urls(query: str, limit: int = 10) -> list[str]:
    """经 DD-04 search 找候选入口 URL（仅入口，不作事实）。"""
    res = provider_call("search", "web_search", {"query": query, "count": limit})
    results = (res.data or {}).get("results", []) if res.ok else []
    return [r.get("url") for r in results if r.get("url")][:limit]


def _city_name(city_code: str, session: Session) -> str:
    return session.execute(
        text("SELECT name FROM city_playbook WHERE city_code = :c"), {"c": city_code}
    ).scalar() or city_code


def _weekend_label(weekend) -> str:
    if not weekend:
        return "本周末"
    start = getattr(weekend, "start", None)
    return f"{start:%m月%d日}周末" if start else "本周末"


def research_weekend_activities(city_code: str, weekend, interests: list[str] | None = None,
                                session: Session | None = None, allow_fetch=None) -> dict:
    """补搜官方入口 → 注册 official_venue 源 → 抓取抽取定级。"""
    own = session is None
    s = session or SessionLocal()
    try:
        city_name = _city_name(city_code, s)
        query = (f"{city_name} {_weekend_label(weekend)} "
                 f"{' '.join(interests or ['展览', '演出', '市集'])} 官方 场馆 门票")
        urls = discover_official_urls(query)
        discovered = 0
        ingested: list[int] = []
        for url in urls:
            if not is_official_like(url):
                continue
            src = ensure_source(url, city_code, SourceType.official_venue, s)
            ingested += process_source(src, s, weekend=weekend, allow_fetch=allow_fetch)
            discovered += 1
        if own:
            s.commit()
    finally:
        if own:
            s.close()
    return {"discovered_sources": discovered, "ingested": ingested}
