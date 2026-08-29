"""pytest 公共夹具。

数据库测试连接 Docker 隔离实例（端口 5433）；每个用例包在事务里，结束回滚，不留脏数据。

**测试强制离线**：在导入 wheretogo 之前用环境变量覆盖 .env（env 优先级高于 .env 文件），
关闭真实模型/深研/外部 key，保证测试确定性、快速、不联网（不受 .env 真实 key 影响）。
"""
from __future__ import annotations

import os

# —— 必须在 import wheretogo 之前：强制测试走确定性兜底 ——
for _k in ("WTG_USE_REAL_MODELS", "WTG_DEEP_RESEARCH_ENABLED"):
    os.environ[_k] = "false"
for _k in ("WTG_LLM_API_KEY", "WTG_EMBEDDING_API_KEY", "WTG_SEARCH_API_KEY",
           "WTG_DASHSCOPE_API_KEY", "WTG_AMAP_KEY", "WTG_QWEATHER_KEY"):
    os.environ[_k] = ""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from wheretogo.db import engine
from wheretogo.enums import VerificationStatus
from wheretogo.models import Activity
from wheretogo.retrieval.providers import HashingEmbeddingProvider

# 固定的目标周末窗口（用于检索测试）
WK_START = datetime(2026, 7, 24, 0, 0, tzinfo=timezone.utc)
WK_END = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
_EMBEDDER = HashingEmbeddingProvider()


@pytest.fixture
def connection():
    conn = engine.connect()
    txn = conn.begin()
    try:
        yield conn
    finally:
        txn.rollback()
        conn.close()


@pytest.fixture
def session(connection) -> Session:
    """绑定到外层事务的会话；用 SAVEPOINT，测试内可提交/回滚而外层最终回滚。"""
    s = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def weekend():
    from wheretogo.retrieval import Weekend

    return Weekend(WK_START, WK_END)


@pytest.fixture
def make_activity(session):
    """活动工厂：插入一条带 embedding 的活动（默认官方确认态、落在目标周末）。"""

    def _make(
        title: str,
        *,
        city_code: str = "310000",
        venue: str = "",
        category: str = "展览",
        price_text: str | None = None,
        verification_status: VerificationStatus = VerificationStatus.official_source_confirmed,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        with_embedding: bool = True,
    ) -> Activity:
        start_at = start_at or datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc)
        emb = None
        if with_embedding:
            emb = _EMBEDDER.embed([f"{title} {venue} {category} {price_text or ''}"])[0]
        a = Activity(
            title=title,
            city_code=city_code,
            venue=venue,
            category=category,
            price_text=price_text,
            evidence={
                "source_type": "official_venue",
                "verification_status": verification_status.value,
                "confidence": 0.9,
            },
            verification_status=verification_status,
            embedding=emb,
            start_at=start_at,
            end_at=end_at,
            expires_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
        )
        session.add(a)
        session.flush()
        return a

    return _make
