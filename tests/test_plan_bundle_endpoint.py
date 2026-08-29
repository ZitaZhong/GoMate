"""GET /plans/{id}/bundle：从 trip_bundles 表恢复探索版/确认版 bundle。

DD-19 联调补的端点：图 checkpoint 的 state values 不含 bundle 大对象
（interrupt/done 时仅由 _persist_event 落库），web-v2 plan 页刷新后经 /state
拿不到 bundle，用本端点兜底恢复。
"""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from wheretogo.bff.app import app
from wheretogo.db import get_session
from wheretogo.models import Plan, TripBundle

client = TestClient(app)


def test_plan_bundle_returns_latest_per_version():
    now = datetime.now(timezone.utc)
    with get_session() as s:
        p = Plan(stage="explore", thread_id=f"plan:test-bundle-{now.timestamp()}",
                 constraints={})
        s.add(p)
        s.flush()
        s.add(TripBundle(plan_id=p.id, version="explore", payload={"theme": "旧版"},
                         created_at=now - timedelta(minutes=2)))
        s.add(TripBundle(plan_id=p.id, version="explore", payload={"theme": "新版"},
                         created_at=now - timedelta(minutes=1)))
        s.add(TripBundle(plan_id=p.id, version="confirm", payload={"theme": "确认版"},
                         created_at=now))
        s.commit()
        pid = p.id
    try:
        r = client.get(f"/plans/{pid}/bundle")
        assert r.status_code == 200
        body = r.json()
        assert body["explore"]["theme"] == "新版"  # 同 version 取最新一条
        assert body["confirm"]["theme"] == "确认版"
    finally:
        with get_session() as s:
            s.query(TripBundle).filter_by(plan_id=pid).delete()
            s.query(Plan).filter_by(id=pid).delete()
            s.commit()


def test_plan_bundle_empty_when_no_rows():
    with get_session() as s:
        p = Plan(stage="explore", thread_id="plan:test-bundle-empty", constraints={})
        s.add(p)
        s.commit()
        pid = p.id
    try:
        r = client.get(f"/plans/{pid}/bundle")
        assert r.status_code == 200
        assert r.json() == {}
    finally:
        with get_session() as s:
            s.query(Plan).filter_by(id=pid).delete()
            s.commit()


def test_plan_bundle_404_for_unknown_plan():
    assert client.get("/plans/999999999/bundle").status_code == 404
