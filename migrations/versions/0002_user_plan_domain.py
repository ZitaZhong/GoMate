"""0002 用户与计划域表（DD-01 §6）。"""
from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
          id           BIGSERIAL PRIMARY KEY,
          anon_id      TEXT UNIQUE NOT NULL,
          created_at   TIMESTAMPTZ DEFAULT now(),
          last_seen_at TIMESTAMPTZ
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_context (
          user_id            BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          home_cities        JSONB DEFAULT '[]',
          budget_band        JSONB,
          prefer_flight      BOOLEAN,
          accept_night_train BOOLEAN,
          interests          JSONB DEFAULT '[]',
          dietary            JSONB DEFAULT '[]',
          visited            JSONB DEFAULT '[]',
          updated_at         TIMESTAMPTZ DEFAULT now()
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS plans (
          id                BIGSERIAL PRIMARY KEY,
          organizer_user_id BIGINT REFERENCES users(id),
          stage             plan_stage NOT NULL DEFAULT 'explore',
          thread_id         TEXT UNIQUE NOT NULL,
          constraints       JSONB NOT NULL DEFAULT '{}',
          weekend_start     TIMESTAMPTZ,
          weekend_end       TIMESTAMPTZ,
          created_at        TIMESTAMPTZ DEFAULT now(),
          updated_at        TIMESTAMPTZ DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_plans_organizer_created "
        "ON plans (organizer_user_id, created_at DESC);"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS plan_members (
          id           BIGSERIAL PRIMARY KEY,
          plan_id      BIGINT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
          invite_token TEXT UNIQUE,
          anon_label   TEXT,
          is_organizer BOOLEAN DEFAULT FALSE,
          joined_at    TIMESTAMPTZ
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS party_constraints (
          id                 BIGSERIAL PRIMARY KEY,
          plan_id            BIGINT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
          member_id          BIGINT REFERENCES plan_members(id) ON DELETE CASCADE,
          origin_area        TEXT,
          origin_geo         GEOGRAPHY(Point,4326),
          earliest_depart    TIMESTAMPTZ,
          latest_return      TIMESTAMPTZ,
          budget_band        JSONB,
          prefer_flight      BOOLEAN,
          accept_night_train BOOLEAN,
          prefs              JSONB DEFAULT '[]',
          dietary            JSONB DEFAULT '[]',
          created_at         TIMESTAMPTZ DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_party_constraints_plan ON party_constraints (plan_id);")


def downgrade() -> None:
    for tbl in ("party_constraints", "plan_members", "plans", "user_context", "users"):
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE;")
