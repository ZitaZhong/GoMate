"""DD-06 活动情报流水线（批量预热 + v2 实时入库 ingest_realtime）。

`activities` 唯一写入方；规划流只读。对外入口：
  - ingest_realtime / ingest_user_url（v2 同步入库，供 DD-17）
  - crawl_source / dispatch_due_sources（批量调度）
  - research_weekend_activities（补搜官方入口）
  - expire_activities（过期下架）
"""
from __future__ import annotations

from .dedup import make_fingerprint
from .extract import ActivityDraft
from .grade import grade_activity
from .ingest import (
    enqueue_review,
    expire_activities,
    ingest_realtime,
    ingest_user_url,
    process_source,
    upsert_activity,
)
from .normalize import NormActivity, normalize_activity
from .research import discover_official_urls, is_official_like, research_weekend_activities
from .scheduler import crawl_source, dispatch_due_sources, due_sources

__all__ = [
    "ActivityDraft", "NormActivity",
    "make_fingerprint", "normalize_activity", "grade_activity",
    "upsert_activity", "enqueue_review", "expire_activities", "process_source",
    "ingest_realtime", "ingest_user_url",
    "crawl_source", "dispatch_due_sources", "due_sources",
    "research_weekend_activities", "discover_official_urls", "is_official_like",
]
