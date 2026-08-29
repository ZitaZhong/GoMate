"""0007 GoMate 活动房间（DD-18）：rooms / room_members / theme_votes / room_itineraries。

- DD-18 §2.1：`rooms`（房间状态机 8 态，TEXT + CHECK，不新增 PG ENUM）
- DD-18 §2.2：`room_members`（成员信息 + 出发地 Geography 坐标；member_token 轻量认证）
- DD-18 §2.3：`theme_votes`（主题投票，UNIQUE(room_id, member_id, theme)）
- DD-18 §2.4：`room_itineraries`（行程版本，is_current 单版本标记）

全程 `IF NOT EXISTS` 幂等；只动隔离库 `wheretogo`；坐标沿用既有 Geography(POINT,4326)
（与 party_constraints.origin_geo 一致，替代文档中的 GEOMETRY，文档已同步说明）。
"""
from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # —— DD-18 §2.1 活动房间 ——
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS rooms (
          id            BIGSERIAL PRIMARY KEY,
          status        TEXT NOT NULL DEFAULT 'DRAFT',
          activity_date DATE NOT NULL,
          city          TEXT NOT NULL DEFAULT '上海',
          time_window   JSONB,                     -- {"earliest": "14:00", "latest": "21:00"}
          budget_range  JSONB,                     -- {"min": 0, "max": 200, "currency": "CNY"}
          theme         TEXT,                      -- 确定后的主题
          theme_method  TEXT,                      -- direct|vote|ai|wheel
          wheel_spins   INT NOT NULL DEFAULT 0,    -- 转盘次数（支持一次反悔=最多2次）
          creator_id    TEXT NOT NULL,
          plan_id       BIGINT REFERENCES plans(id) ON DELETE SET NULL,
          thread_id     TEXT NOT NULL,             -- LangGraph thread_id = 'room:{id}'
          invite_code   TEXT NOT NULL UNIQUE,
          created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
          expire_at     TIMESTAMPTZ NOT NULL,      -- 活动结束后 7 天
          CONSTRAINT valid_room_status CHECK (status IN (
            'DRAFT','COLLECTING','THEME_SELECTING','RECOMMENDING',
            'ACTIVITY_SELECTED','PLANNING','PUBLISHED','EXPIRED'))
        );
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_rooms_status_expire ON rooms (status, expire_at);")

    # —— DD-18 §2.2 房间成员 ——
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS room_members (
          id               BIGSERIAL PRIMARY KEY,
          room_id          BIGINT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
          nickname         TEXT NOT NULL,
          member_token     TEXT NOT NULL UNIQUE,   -- 轻量认证
          is_creator       BOOLEAN NOT NULL DEFAULT FALSE,
          origin_name      TEXT,                   -- 出发地名称（地铁站/商圈）
          origin_geo       GEOGRAPHY(POINT, 4326), -- 出发地坐标（与 party_constraints 一致）
          origin_poi_id    TEXT,                   -- 高德 POI ID
          earliest_depart  TEXT,                   -- "14:00"
          latest_end       TEXT,                   -- "21:00"
          budget           INT,                    -- 人均预算（分）
          interests        TEXT[],                 -- 兴趣标签
          hard_constraints TEXT[],                 -- 硬性约束
          negative_prefs   TEXT[],                 -- 不接受的
          transport_pref   TEXT,                   -- walk|transit|drive|any
          note             TEXT,                   -- 自由备注
          submitted_at     TIMESTAMPTZ,
          created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_room_members_room ON room_members (room_id);")

    # —— DD-18 §2.3 主题投票 ——
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS theme_votes (
          id         BIGSERIAL PRIMARY KEY,
          room_id    BIGINT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
          member_id  BIGINT NOT NULL REFERENCES room_members(id) ON DELETE CASCADE,
          theme      TEXT NOT NULL,
          weight     INT NOT NULL DEFAULT 1,       -- 1=可接受, 3=强烈喜欢, -2=不喜欢
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE(room_id, member_id, theme)
        );
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_theme_votes_room ON theme_votes (room_id);")

    # —— DD-18 §2.4 房间行程版本 ——
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS room_itineraries (
          id         BIGSERIAL PRIMARY KEY,
          room_id    BIGINT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
          version    INT NOT NULL DEFAULT 1,
          payload    JSONB NOT NULL,               -- 完整行程 JSON
          is_current BOOLEAN NOT NULL DEFAULT TRUE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_room_itineraries_room_current "
        "ON room_itineraries (room_id, is_current);"
    )


def downgrade() -> None:
    for tbl in ("room_itineraries", "theme_votes", "room_members", "rooms"):
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE;")
