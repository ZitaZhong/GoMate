"""Phase 0 验收：v2 新增 schema（DD-16/17/06）+ plans.conversation + 模型 round-trip。

锁定迁移 0005 产物：长期记忆表、深研作业/缓存、活动审核队列、conversation 列、索引。
"""
from __future__ import annotations

from sqlalchemy import text

from wheretogo.config import get_settings
from wheretogo.models import ActivityReviewQueue, DeepResearchCache, DeepResearchJob, UserMemory
from wheretogo.retrieval.providers import (
    DashScopeEmbeddingProvider,
    DashScopeRerankProvider,
    HashingEmbeddingProvider,
    LexicalRerankProvider,
    get_embedding_provider,
    get_rerank_provider,
)

V2_TABLES = {"user_memory", "deep_research_jobs", "deep_research_cache", "activity_review_queue"}


def test_v2_tables_created(session):
    got = set(
        session.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars()
    )
    assert V2_TABLES.issubset(got)


def test_plans_conversation_column(session):
    cols = set(
        session.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name='plans'")
        ).scalars()
    )
    assert "conversation" in cols


def test_user_memory_indexes(session):
    idx = set(
        session.execute(text("SELECT indexname FROM pg_indexes WHERE tablename='user_memory'")).scalars()
    )
    assert {"ix_user_memory_embedding_hnsw", "ix_user_memory_user_valid"} <= idx


def test_user_memory_round_trip(session):
    m = UserMemory(
        mem_type="preference",
        key="diet",
        content="偏好看展览、不吃辣",
        confidence=0.8,
        valid=True,
    )
    session.add(m)
    session.flush()
    assert m.id is not None
    assert session.get(UserMemory, m.id).content == "偏好看展览、不吃辣"


def test_user_memory_soft_invalidate(session):
    """覆盖语义：同 key 旧记录置 valid=FALSE（软失效）。"""
    session.add(UserMemory(mem_type="preference", key="diet", content="不吃辣", valid=True))
    session.flush()
    # 模拟 write_memory 的"同键覆盖"：旧行 valid=False + 新行
    session.execute(
        text("UPDATE user_memory SET valid=FALSE WHERE key='diet' AND valid=TRUE")
    )
    session.add(UserMemory(mem_type="preference", key="diet", content="能吃辣了", valid=True))
    session.flush()
    valid = session.execute(
        text("SELECT content FROM user_memory WHERE key='diet' AND valid=TRUE")
    ).scalars()
    assert list(valid) == ["能吃辣了"]


def test_deep_research_job_round_trip(session):
    j = DeepResearchJob(trigger="user_explicit", query={"city": "310000", "categories": ["展览"]})
    session.add(j)
    session.flush()
    assert j.status == "running"  # server_default
    assert session.get(DeepResearchJob, j.id).query["city"] == "310000"


def test_deep_research_cache_round_trip(session):
    c = DeepResearchCache(query_hash="abc123", result_ids=[1, 2, 3])
    session.add(c)
    session.flush()
    assert session.get(DeepResearchCache, "abc123").result_ids == [1, 2, 3]


def test_activity_review_queue_round_trip(session):
    q = ActivityReviewQueue(reason="quote_mismatch", draft={"title": "草稿"})
    session.add(q)
    session.flush()
    assert q.status == "pending"  # server_default
    assert session.get(ActivityReviewQueue, q.id).reason == "quote_mismatch"


def test_provider_factories_default_to_deterministic_fallback():
    """无 key（默认 dev）→ embedding/rerank 走确定性兜底，离线可测。"""
    s = get_settings()
    assert not s.dashscope_api_key  # 测试环境无 key
    assert isinstance(get_embedding_provider(), HashingEmbeddingProvider)
    assert isinstance(get_rerank_provider(), LexicalRerankProvider)


def test_dashscope_provider_classes_importable():
    # DashScope API provider 已就位（实际调用由 Phase 1 ResilientProvider 包裹）
    assert DashScopeEmbeddingProvider is not None
    assert DashScopeRerankProvider is not None
