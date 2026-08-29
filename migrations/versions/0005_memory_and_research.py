"""0005 v2 新增表：长期记忆 / 深研作业与缓存 / 活动审核队列 + plans.conversation。

- DD-16 §3：`user_memory`（Mem0 风格语义记忆 + pgvector HNSW + 软失效覆盖语义）
- DD-17 §5：`deep_research_jobs` / `deep_research_cache`（深研作业与短 TTL 缓存）
- DD-06 §5.9：`activity_review_queue`（抽取/冲突/低置信人工复核台）
- DD-15 §3.1：`plans.conversation`（多轮消息持久化兜底，主存仍在 checkpoint）

全程 `IF NOT EXISTS` 幂等；只动隔离库 `wheretogo`；不新增 PG ENUM（用 TEXT + 应用枚举校验）。
"""
from __future__ import annotations

from alembic import op

from wheretogo.config import get_settings

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_EMBED_DIM = get_settings().embedding_dim


def upgrade() -> None:
    # —— DD-16 §3 长期语义记忆 ——
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS user_memory (
          id             BIGSERIAL PRIMARY KEY,
          user_id        BIGINT REFERENCES users(id) ON DELETE CASCADE,
          mem_type       TEXT NOT NULL,             -- preference / fact / episodic
          key            TEXT,                      -- 归一化键(diet/origin_city/budget/interest)用于覆盖
          content        TEXT NOT NULL,             -- 自然语言记忆
          embedding      VECTOR({_EMBED_DIM}),      -- 语义召回
          confidence     REAL DEFAULT 0.7,
          source_plan_id BIGINT,                    -- 来自哪次会话（可溯源）
          valid          BOOLEAN DEFAULT TRUE,      -- 覆盖时旧记忆置 FALSE（软失效、可回溯）
          created_at     TIMESTAMPTZ DEFAULT now(),
          updated_at     TIMESTAMPTZ DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_memory_embedding_hnsw "
        "ON user_memory USING hnsw (embedding vector_cosine_ops);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_memory_user_valid "
        "ON user_memory (user_id, mem_type, valid);"
    )

    # —— DD-17 §5 深研作业 ——
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS deep_research_jobs (
          id                 BIGSERIAL PRIMARY KEY,
          plan_id            BIGINT REFERENCES plans(id) ON DELETE CASCADE,
          trigger            TEXT NOT NULL,          -- user_explicit/coverage_gap/stale/long_tail_city
          query              JSONB NOT NULL,         -- {city, weekend, categories, nl}
          status             TEXT DEFAULT 'running', -- running/succeeded/partial/failed/timeout
          found_activity_ids BIGINT[],               -- 入库的 activities.id
          source_count       INT DEFAULT 0,
          official_count     INT DEFAULT 0,
          started_at         TIMESTAMPTZ DEFAULT now(),
          finished_at        TIMESTAMPTZ,
          error              TEXT,
          CONSTRAINT ck_drj_query_obj CHECK (jsonb_typeof(query) = 'object')
        );
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_deep_research_jobs_plan ON deep_research_jobs (plan_id);")

    # —— DD-17 §5 深研缓存（相同查询短期复用）——
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS deep_research_cache (
          query_hash   TEXT PRIMARY KEY,             -- sha1(city+weekend+categories 归一)
          result_ids   BIGINT[],
          source_list  JSONB,
          created_at   TIMESTAMPTZ DEFAULT now(),
          expires_at   TIMESTAMPTZ                   -- 当周数据短 TTL(如 6h)
        );
        """
    )

    # —— DD-06 §5.9 活动审核队列（抽取失败/冲突/低置信/quote_mismatch/geocode_failed）——
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_review_queue (
          id            BIGSERIAL PRIMARY KEY,
          raw_page_id   BIGINT REFERENCES raw_pages(id) ON DELETE CASCADE,
          source_id     BIGINT REFERENCES source_registry(id) ON DELETE SET NULL,
          reason        TEXT NOT NULL,
          draft         JSONB,                       -- 抽取草稿（ActivityDraft dump，含 evidence_quote）
          conflict_with BIGINT REFERENCES activities(id) ON DELETE SET NULL,
          status        TEXT DEFAULT 'pending',      -- pending/approved/rejected/merged
          reviewer      TEXT,
          resolved_at   TIMESTAMPTZ,
          created_at    TIMESTAMPTZ DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_activity_review_status_created "
        "ON activity_review_queue (status, created_at);"
    )

    # —— DD-15 §3.1 plans.conversation（多轮消息持久化兜底）——
    op.execute("ALTER TABLE plans ADD COLUMN IF NOT EXISTS conversation JSONB DEFAULT '[]';")


def downgrade() -> None:
    op.execute("ALTER TABLE plans DROP COLUMN IF EXISTS conversation;")
    for tbl in (
        "activity_review_queue",
        "deep_research_cache",
        "deep_research_jobs",
        "user_memory",
    ):
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE;")
