"""P0 修复回归：① latest_return 不再死循环（裁剪+熔断）② 上海→杭州出跨城交通 ③ ICS 本地时区。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wheretogo.domain.compose import build_ics
from wheretogo.domain.timeline import solve_timeline
from wheretogo.domain.transport import _same_city, build_transport_options
from wheretogo.orchestration import PlannerService
from wheretogo.orchestration.graph import route_after_validate

_SH_ORIGIN = {"city_code": "310000", "name": "上海", "center": [121.473, 31.230]}
_HZ_CARD = {"city_code": "330100", "name": "杭州", "center": [120.155, 30.274]}


# —— P0-1：上海→杭州 跨城（不再被 "上海" 硬编码判同城）——
def test_same_city_by_city_code_not_hardcoded_shanghai():
    assert _same_city({"target_city_code": "330100"}, _SH_ORIGIN) is False  # 上海→杭州 = 跨城
    assert _same_city({"target_city_code": "310000"}, _SH_ORIGIN) is True   # 上海→上海 = 同城


def test_shanghai_to_hangzhou_produces_cross_city_mode():
    c = {"target_city_code": "330100", "origins": ["上海"],
         "weekend_start": "2026-08-08", "weekend_end": "2026-08-10"}
    opts = build_transport_options(c, [_HZ_CARD], origin=_SH_ORIGIN)
    cand = opts["candidates"][0]
    assert cand["recommended_mode"] != "local"  # 跨城：不是 local
    assert cand["recommended_mode"] in {"compare", "rail", "air"}  # decide_mode 距离带判断
    assert "rail" in cand["door_to_door"] and "air" in cand["door_to_door"]
    # 门到门不再是 0（P2-6：有真实估算）
    assert cand["door_to_door"]["rail"]["total_min"] > 0
    assert opts["prefill"]["rail"]["to"] == "杭州"
    assert opts["presale"]  # 起售提醒


# —— P0-2：latest_return 不死循环（裁剪 + 熔断）——
def test_solve_timeline_trims_activities_past_latest_return():
    base = (datetime.now(timezone.utc) + timedelta(days=7)).replace(
        hour=20, minute=0, second=0, microsecond=0
    )  # future evening activity; independent of wall-clock date
    late = {"title": "晚场演出", "start_at": base.isoformat(),
            "end_at": (base + timedelta(hours=3)).isoformat(), "id": 1,
            "evidence": {"source_type": "official_venue", "verification_status": "official_source_confirmed"}}
    early = {"title": "上午展", "start_at": (base - timedelta(hours=10)).isoformat(),
             "end_at": (base - timedelta(hours=8)).isoformat(), "id": 2,
             "evidence": {"source_type": "official_venue", "verification_status": "official_source_confirmed"}}
    lr = (base + timedelta(hours=1)).isoformat()  # 最晚返程在晚场结束前
    slots = solve_timeline([late, early], [], [], {"latest_return": lr})
    titles = [s["title"] for s in slots if s.get("kind") == "activity"]
    assert "上午展" in titles and "晚场演出" not in titles  # 超过 latest_return 被裁剪


def test_route_after_validate_caps_and_forces_compose():
    # attempts 达上限 → 强制 ok（熔断，不再 reflow/retransport）
    assert route_after_validate({"validation": {"ok": False, "issues": ["RETURN_TIGHT"],
                                                "metrics": {"attempts": 3}}}) == "ok"
    # 未达上限仍正常路由
    assert route_after_validate({"validation": {"ok": False, "issues": ["RETURN_TIGHT"],
                                                "metrics": {"attempts": 1}}}) == "retransport"


def test_e2e_latest_return_completes_not_loops():
    """端到端：上海同城 + latest_return → resume 酒店 → 必须到达 confirm（不死循环）。"""
    svc = PlannerService()
    c = {"query": "周末 展览", "interests": ["展览"], "target_city_code": "310000",
         "latest_return": "2026-07-27T22:00:00+08:00"}
    svc.start("p0-2", c)
    booking = [{"kind": "hotel", "extracted": {"name": "示例酒店"},
                "evidence": {"source_type": "user_provided", "verification_status": "confirmed_by_user",
                             "confidence": 1.0}}]
    st = svc.resume("p0-2", booking)["state"]
    assert st["stage"] == "confirm"  # 没死循环，出了确认版
    assert st["bundle"]["version"] == "confirm"


# —— P1-4 / C1：ICS 本地时区（不再 UTC Z 偏 8h）——
def test_ics_uses_shanghai_timezone_not_utc_z():
    ics = build_ics({"timeline": [{"kind": "activity", "title": "展",
                                   "start_at": "2026-07-25T10:00:00+08:00",
                                   "end_at": "2026-07-25T12:00:00+08:00", "ref_id": 1}]})
    assert "TZID:Asia/Shanghai" in ics
    assert "T100000Z" not in ics  # 不再是 UTC Z（当地 10 点不再标成 UTC 10 点）
    assert "BEGIN:VTIMEZONE" in ics
