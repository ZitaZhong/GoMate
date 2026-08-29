"""BFF 端到端冒烟（非流式路由）：/health /plans /chat /calendar.ics /bookings/import。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from wheretogo.bff.app import app

client = TestClient(app)


def test_health():
    assert client.get("/health").json() == {"ok": True}


def test_create_plan_and_state():
    r = client.post("/plans", json={"constraints": {"query": "周末 展览", "target_city_code": "310000"}})
    assert r.status_code == 200
    pid = r.json()["plan_id"]
    assert client.get(f"/plans/{pid}/state").status_code == 200


def test_chat_route_classifies_confirm():
    pid = client.post("/plans", json={"constraints": {}}).json()["plan_id"]
    d = client.post(f"/plans/{pid}/chat", json={"message": "我买好票了"}).json()
    assert d["intent"] == "confirm_booking" and d["action"] == "resume"


def test_chat_first_autocreates_plan_and_remembers():
    """chat-first：首条带约束消息自动建 plan；第二轮“本周末”跨轮记住出发地并齐备。"""
    d1 = client.post("/plans/new/chat", json={"message": "我从上海出发看演唱会"}).json()
    pid = d1.get("plan_id")
    assert pid and pid.isdigit()  # 首条带约束→自动建 plan
    assert "上海" in (d1["constraints"].get("origins") or [])
    d2 = client.post(f"/plans/{pid}/chat", json={"message": "本周末"}).json()
    slots = [c["slot"] for c in d2.get("pending_clarify", [])]
    assert "origins" not in slots  # 跨轮记住出发地，不重复追问
    assert d2.get("ready_to_plan") is True  # 约束齐备 → 可自动生成


def test_chat_ask_info_no_plan_no_crash():
    """ask_info 在无 plan（city=None）时不再 500（修 AmbiguousParameter），且永不抛异常。"""
    r = client.post("/plans/new/chat", json={"message": "莫奈展门票多少钱？"})
    assert r.status_code == 200 and "reply" in r.json()


def test_calendar_ics_always_200():
    pid = client.post("/plans", json={"constraints": {"target_city_code": "310000"}}).json()["plan_id"]
    r = client.get(f"/plans/{pid}/calendar.ics")
    assert r.status_code == 200 and "VCALENDAR" in r.text


def test_unknown_plan_returns_404():
    """P2-4：不存在的 plan_id → 404（不再是 200 空数据）。"""
    assert client.get("/plans/999999/state").status_code == 404
    assert client.get("/plans/999999/calendar.ics").status_code == 404


def test_anchor_times_from_booking():
    """P2-5：交通锚点带回填时刻（date+dep_time → start_at）。"""
    from wheretogo.domain.backfill import to_timeline_anchors
    anchors = to_timeline_anchors([{"kind": "train", "extracted": {
        "from_station": "上海虹桥", "to_station": "杭州东", "date": "2026-08-08", "dep_time": "8:00"},
        "evidence": {}}])
    assert anchors and anchors[0]["start_at"] is not None
    assert "上海虹桥" in anchors[0]["title"] and "8:00" in anchors[0]["title"]


def test_import_booking_ready():
    pid = client.post("/plans", json={"constraints": {}}).json()["plan_id"]
    r = client.post(f"/plans/{pid}/bookings/import", json={
        "kind": "train",
        "extracted": {"from_station": "上海", "to_station": "北京", "date": "2026-08-09"}})
    assert r.status_code == 200
    assert r.json()["ready_for_resume"] is True
