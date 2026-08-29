"""0003 城市与活动域表 + 检索索引（DD-01 §7，增补 A）。

活动表是检索核心：generated `search_tsv`（BM25/全文）+ `embedding`（HNSW 稠密）
+ `location`（GiST 地理）+ 部分索引（确认态活动），支撑 DD-05 三重过滤召回。
"""
from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS city_playbook (
          id            BIGSERIAL PRIMARY KEY,
          city_code     TEXT UNIQUE NOT NULL,
          name          TEXT NOT NULL,
          center        GEOGRAPHY(Point,4326),
          stations      JSONB,
          lodging_areas JSONB,
          hubs          JSONB,
          transit_notes JSONB,
          weekend_tags  TEXT[],
          seasonal_risk JSONB,
          updated_at    TIMESTAMPTZ DEFAULT now()
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS venues (
          id           BIGSERIAL PRIMARY KEY,
          city_code    TEXT REFERENCES city_playbook(city_code),
          name         TEXT NOT NULL,
          location     GEOGRAPHY(Point,4326),
          category     TEXT,
          official_url TEXT,
          CONSTRAINT uq_venues_city_name UNIQUE (city_code, name)
        );
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_venues_location_gist ON venues USING GIST (location);")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS source_registry (
          id             BIGSERIAL PRIMARY KEY,
          name           TEXT NOT NULL,
          city_code      TEXT,
          source_type    source_type NOT NULL,
          entry_url      TEXT NOT NULL,
          parser_kind    TEXT,
          fetch_interval INTERVAL DEFAULT '1 day',
          robots_ok      BOOLEAN DEFAULT TRUE,
          trust_level    INT DEFAULT 3,
          enabled        BOOLEAN DEFAULT TRUE,
          last_fetched_at TIMESTAMPTZ
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_pages (
          id           BIGSERIAL PRIMARY KEY,
          source_id    BIGINT REFERENCES source_registry(id) ON DELETE CASCADE,
          url          TEXT NOT NULL,
          http_status  INT,
          content_hash TEXT,
          etag         TEXT,
          clean_md     TEXT,
          fetched_at   TIMESTAMPTZ DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_raw_pages_source_fetched "
        "ON raw_pages (source_id, fetched_at DESC);"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS activities (
          id                  BIGSERIAL PRIMARY KEY,
          fingerprint         TEXT UNIQUE,
          title               TEXT NOT NULL,
          city_code           TEXT REFERENCES city_playbook(city_code),
          venue_id            BIGINT REFERENCES venues(id),
          venue               TEXT,
          location            GEOGRAPHY(Point,4326),
          start_at            TIMESTAMPTZ,
          end_at              TIMESTAMPTZ,
          price_text          TEXT,
          booking_url         TEXT,
          category            TEXT,
          evidence            JSONB NOT NULL,
          verification_status verification_status NOT NULL,
          availability_status availability_status DEFAULT 'user_must_confirm',
          embedding           VECTOR(1024),
          embedding_version   TEXT DEFAULT 'bge-m3-v1',
          search_tsv          TSVECTOR GENERATED ALWAYS AS (
                                 to_tsvector('simple',
                                   coalesce(title,'') || ' ' || coalesce(venue,''))) STORED,
          expires_at          TIMESTAMPTZ,
          created_at          TIMESTAMPTZ DEFAULT now(),
          updated_at          TIMESTAMPTZ DEFAULT now(),
          CONSTRAINT ck_activities_evidence_obj CHECK (jsonb_typeof(evidence) = 'object')
        );
        """
    )
    # —— 检索索引（增补 A / DD-05）——
    op.execute("CREATE INDEX IF NOT EXISTS ix_activities_location_gist ON activities USING GIST (location);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_activities_city_start ON activities (city_code, start_at);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_activities_search_tsv ON activities USING GIN (search_tsv);")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_activities_embedding_hnsw "
        "ON activities USING hnsw (embedding vector_cosine_ops);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_activities_verif_partial ON activities (verification_status) "
        "WHERE verification_status IN ('official_source_confirmed','public_source_observed');"
    )


def downgrade() -> None:
    for tbl in ("activities", "raw_pages", "source_registry", "venues", "city_playbook"):
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE;")
