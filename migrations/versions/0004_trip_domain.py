"""0004 回填与行程域表 + 索引（DD-01 §8）。"""
from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
          id           BIGSERIAL PRIMARY KEY,
          plan_id      BIGINT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
          kind         booking_kind NOT NULL,
          raw_input    TEXT,
          input_kind   TEXT,
          extracted    JSONB,
          evidence     JSONB NOT NULL,
          confirmed    BOOLEAN DEFAULT FALSE,
          confirmed_at TIMESTAMPTZ,
          created_at   TIMESTAMPTZ DEFAULT now(),
          CONSTRAINT ck_bookings_evidence_obj CHECK (jsonb_typeof(evidence) = 'object')
        );
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_bookings_plan_kind ON bookings (plan_id, kind);")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dining_picks (
          id          BIGSERIAL PRIMARY KEY,
          plan_id     BIGINT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
          amap_poi_id TEXT,
          name        TEXT NOT NULL,
          location    GEOGRAPHY(Point,4326),
          cuisine     TEXT,
          price_band  JSONB,
          open_hours  TEXT,
          phone       TEXT,
          meal_slot   TEXT,
          is_fallback BOOLEAN DEFAULT FALSE,
          evidence    JSONB NOT NULL,
          created_at  TIMESTAMPTZ DEFAULT now(),
          CONSTRAINT ck_dining_evidence_obj CHECK (jsonb_typeof(evidence) = 'object')
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS route_legs (
          id         BIGSERIAL PRIMARY KEY,
          plan_id    BIGINT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
          seq        INT,
          from_label TEXT,
          to_label   TEXT,
          from_geo   GEOGRAPHY(Point,4326),
          to_geo     GEOGRAPHY(Point,4326),
          mode       TEXT,
          minutes    INT,
          distance_m INT,
          evidence   JSONB NOT NULL,
          created_at TIMESTAMPTZ DEFAULT now(),
          CONSTRAINT ck_route_legs_evidence_obj CHECK (jsonb_typeof(evidence) = 'object')
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS timeline_slots (
          id        BIGSERIAL PRIMARY KEY,
          plan_id   BIGINT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
          seq       INT NOT NULL,
          start_at  TIMESTAMPTZ,
          end_at    TIMESTAMPTZ,
          kind      slot_kind NOT NULL,
          ref_table TEXT,
          ref_id    BIGINT,
          title     TEXT,
          evidence  JSONB NOT NULL,
          CONSTRAINT uq_timeline_plan_seq UNIQUE (plan_id, seq),
          CONSTRAINT ck_timeline_evidence_obj CHECK (jsonb_typeof(evidence) = 'object')
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS trip_bundles (
          id         BIGSERIAL PRIMARY KEY,
          plan_id    BIGINT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
          version    bundle_version NOT NULL,
          payload    JSONB NOT NULL,
          created_at TIMESTAMPTZ DEFAULT now(),
          CONSTRAINT ck_bundles_payload_obj CHECK (jsonb_typeof(payload) = 'object')
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_trip_bundles_plan_ver_created "
        "ON trip_bundles (plan_id, version, created_at DESC);"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
          id       BIGSERIAL PRIMARY KEY,
          plan_id  BIGINT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
          type     reminder_type NOT NULL,
          fire_at  TIMESTAMPTZ NOT NULL,
          channel  reminder_channel NOT NULL,
          payload  JSONB NOT NULL,
          status   TEXT DEFAULT 'scheduled',
          sent_at  TIMESTAMPTZ,
          CONSTRAINT ck_reminders_payload_obj CHECK (jsonb_typeof(payload) = 'object')
        );
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_reminders_status_fire ON reminders (status, fire_at);")


def downgrade() -> None:
    for tbl in ("reminders", "trip_bundles", "timeline_slots", "route_legs", "dining_picks", "bookings"):
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE;")
