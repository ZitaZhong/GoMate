"""DD-01 §11.4 用户数据权利：一键导出 / 一键删除（级联）验收测试。"""
from __future__ import annotations

from wheretogo.enums import BundleVersion
from wheretogo.models import Plan, TripBundle, User, UserContext
from wheretogo.services import delete_user_data, export_user_data


def _make_user_with_plan(session) -> tuple[int, int]:
    u = User(anon_id="anon-test-privacy")
    session.add(u)
    session.flush()
    session.add(UserContext(user_id=u.id, interests=["展览"], dietary=["不吃辣"]))
    p = Plan(organizer_user_id=u.id, thread_id=f"plan:test:{u.id}", constraints={"query": "展览"})
    session.add(p)
    session.flush()
    session.add(TripBundle(plan_id=p.id, version=BundleVersion.explore, payload={"ok": True}))
    session.flush()
    return u.id, p.id


def test_export_user_data(session):
    uid, _ = _make_user_with_plan(session)
    exp = export_user_data(session, uid)
    assert exp["user"]["anon_id"] == "anon-test-privacy"
    assert exp["context"]["interests"] == ["展览"]
    assert len(exp["plans"]) == 1
    assert exp["plans"][0]["bundles"][0]["payload"] == {"ok": True}


def test_delete_user_data_cascades(session):
    uid, pid = _make_user_with_plan(session)
    res = delete_user_data(session, uid)
    assert res["deleted_plans"] == 1
    session.expire_all()
    assert session.get(User, uid) is None
    assert session.get(Plan, pid) is None  # 级联删除
