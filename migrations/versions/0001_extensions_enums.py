"""0001 扩展 + langgraph schema + 共享枚举（DD-01 §2 / §4 / §9.3）。

- 扩展：postgis、vector 必装；vectorscale 可选（缺失不阻断，v0.1 用 HNSW）。
- langgraph 独立 schema：checkpoint 表由 PostgresSaver 运行时自建（DD-02）。
- 枚举：全系统统一，避免字符串漂移。
"""
from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_ENUMS: dict[str, tuple[str, ...]] = {
    "verification_status": (
        "confirmed_by_user", "official_source_confirmed", "public_source_observed",
        "estimated", "unknown", "expired",
    ),
    "source_type": (
        "official_venue", "culture_bureau", "open_dataset", "search",
        "user_provided", "editorial", "community", "amap", "qweather", "variflight", "llm",
    ),
    "plan_stage": ("explore", "await_booking", "confirm"),
    "transport_mode": ("rail", "air", "mixed"),
    "booking_kind": ("train", "flight", "hotel"),
    "availability_status": ("user_must_confirm", "likely_available", "sold_out", "unknown"),
    "reminder_type": (
        "presale", "activity_booking", "flight_recheck", "pre_trip_72h", "weather_24h",
        "doc_check", "hotel_cancel_deadline", "activity_start", "return_trip",
    ),
    "reminder_channel": ("web_push", "email", "ics"),
    "bundle_version": ("explore", "confirm"),
    "slot_kind": ("transport", "activity", "meal", "lodging", "buffer", "free"),
}


def upgrade() -> None:
    # —— 扩展（迁移首步）——
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    # vectorscale 可选：不可用时仅告警，不中断（DD-01 §10.3 触发条件）
    op.execute(
        """
        DO $$
        BEGIN
            CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE;
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'vectorscale 不可用，跳过（v0.1 用 HNSW 即可）: %', SQLERRM;
        END
        $$;
        """
    )

    # —— LangGraph 检查点独立 schema（表由 PostgresSaver 自建，DD-01 §9.3）——
    op.execute("CREATE SCHEMA IF NOT EXISTS langgraph;")

    # —— 共享枚举（幂等：已存在则跳过）——
    for name, labels in _ENUMS.items():
        values = ", ".join(f"'{v}'" for v in labels)
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = '{name}') THEN
                    CREATE TYPE {name} AS ENUM ({values});
                END IF;
            END
            $$;
            """
        )


def downgrade() -> None:
    for name in reversed(list(_ENUMS)):
        op.execute(f"DROP TYPE IF EXISTS {name};")
    op.execute("DROP SCHEMA IF EXISTS langgraph CASCADE;")
    # 扩展不在降级中删除（可能被其它库共享；这里是隔离实例，保守起见保留）
