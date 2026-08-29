"""多人协作验收（DD-07 §5）：匿名邀请 → 各人填写 → 聚合入 plan.constraints。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from wheretogo.bff.app import app

client = TestClient(app)


def test_invite_fill_aggregate_flow():
    # 1) 组织者建计划
    pid = client.post("/plans", json={"constraints": {"target_city_code": "310000"}}).json()["plan_id"]
    # 2) 生成 2 个邀请
    inv = client.post(f"/plans/{pid}/invites", json={"count": 2}).json()
    assert len(inv["invites"]) == 2
    tokens = [i["token"] for i in inv["invites"]]
    # 3) 同伴打开邀请（不泄露组织者）
    view = client.get(f"/invite/{tokens[0]}").json()
    assert view["plan_id"] == int(pid) and view["anon_label"].startswith("同伴")
    # 无效 token 404
    assert client.get("/invite/nope").status_code == 404
    # 4) 两名同伴填写（不同出发地/预算/时间）
    client.post(f"/invite/{tokens[0]}/constraints", json={
        "origin_area": "北京·朝阳", "earliest_depart": "2026-07-25T08:00:00+08:00",
        "latest_return": "2026-07-27T20:00:00+08:00", "budget_band": {"min": 500, "max": 1500},
        "accept_flight": True, "accept_night_train": False, "interests": ["展览"], "dietary": ["辣"]})
    client.post(f"/invite/{tokens[1]}/constraints", json={
        "origin_area": "上海·徐汇", "earliest_depart": "2026-07-25T06:00:00+08:00",
        "latest_return": "2026-07-27T18:00:00+08:00", "budget_band": {"min": 800, "max": 2000},
        "accept_flight": False, "accept_night_train": True, "interests": ["演出"], "dietary": []})
    # 5) 聚合 → 合并进 plan.constraints（公平性：earliest=max/latest=min/预算交集/兴趣并集）
    agg = client.get(f"/plans/{pid}/party/aggregate").json()
    assert agg["members"] == 2
    a = agg["aggregated"]
    from datetime import datetime
    # 比较时刻（聚合值存 UTC，与 +08:00 同一时刻）
    assert datetime.fromisoformat(a["earliest_depart"]) == datetime.fromisoformat("2026-07-25T08:00:00+08:00")
    assert datetime.fromisoformat(a["latest_return"]) == datetime.fromisoformat("2026-07-27T18:00:00+08:00")
    assert a["budget_band"]["min"] == 800 and a["budget_band"]["max"] == 1500
    assert set(a["interests"]) == {"展览", "演出"}
    # 6) plan.constraints 已被聚合更新
    from wheretogo.db import get_session
    from wheretogo.models import Plan
    with get_session() as s:
        c = s.get(Plan, int(pid)).constraints
    assert c.get("party_size") == 2
    assert set(c.get("origins") or []) == {"北京·朝阳", "上海·徐汇"}
    assert "辣" in (c.get("dietary") or [])
