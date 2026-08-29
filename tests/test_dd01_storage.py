"""DD-01 数据模型与存储层验收测试（对应 DD-01 §14 DoD 的 schema 层）。"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

EXPECTED_ENUMS = {
    "verification_status", "source_type", "plan_stage", "transport_mode", "booking_kind",
    "availability_status", "reminder_type", "reminder_channel", "bundle_version", "slot_kind",
}
EXPECTED_TABLES = {
    "users", "user_context", "plans", "plan_members", "party_constraints",
    "city_playbook", "venues", "source_registry", "raw_pages", "activities",
    "bookings", "dining_picks", "route_legs", "timeline_slots", "trip_bundles", "reminders",
}


def test_all_enums_created(session):
    got = set(session.execute(text("SELECT typname FROM pg_type WHERE typtype='e'")).scalars())
    assert EXPECTED_ENUMS.issubset(got)


def test_all_tables_created(session):
    got = set(
        session.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        ).scalars()
    )
    assert EXPECTED_TABLES.issubset(got)


def test_langgraph_schema_exists(session):
    assert session.scalar(text("SELECT 1 FROM pg_namespace WHERE nspname='langgraph'")) == 1


def test_seed_cities_loaded(session):
    n = session.scalar(text("SELECT count(*) FROM city_playbook"))
    assert n >= 15
    wkt = session.scalar(text("SELECT ST_AsText(center) FROM city_playbook WHERE city_code='310000'"))
    assert wkt and "POINT" in wkt


def test_activities_retrieval_indexes(session):
    idx = set(session.execute(text("SELECT indexname FROM pg_indexes WHERE tablename='activities'")).scalars())
    assert {"ix_activities_embedding_hnsw", "ix_activities_search_tsv", "ix_activities_location_gist"} <= idx


def test_search_tsv_is_generated(session):
    row = session.execute(
        text(
            "INSERT INTO activities (title, venue, evidence, verification_status) "
            "VALUES ('油画展 莫奈特展', '中华艺术宫', '{}'::jsonb, 'official_source_confirmed') "
            "RETURNING search_tsv"
        )
    ).scalar()
    assert row is not None and "莫奈" in row  # 生成列已按 title+venue 填充


def test_evidence_check_constraint_rejects_non_object(session):
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO activities (title, evidence, verification_status) "
                "VALUES ('bad', '\"not-an-object\"'::jsonb, 'unknown')"
            )
        )
        session.flush()
