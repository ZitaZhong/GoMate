"""锚点路线设计（design_itinerary）：意图识别、锚点解析、日路线求解。

对应用户实测场景：「我既要去动漫博物馆，也要去万兽之王巡回演唱会，帮我设计一条路线」
——系统必须具备"点名锚点 → 排路线"的分析设计能力，而不是当普通兴趣重跑推荐。
"""
from datetime import datetime, timezone

from wheretogo.copilot.handle_turn import _looks_like_route_design, classify_intent, handle_turn
from wheretogo.domain.route_design import design_day_route, resolve_anchors

WS = datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc)
WE = datetime(2026, 8, 9, 23, 59, 59, tzinfo=timezone.utc)
USER_MSG = "我既要去动漫博物馆，也要去万兽之王巡回演唱会，帮我设计一条路线"

MUSEUM = {
    "id": 1, "matched_name": "动漫博物馆", "title": "2026杭州中国动漫博物馆暑期免费观影",
    "venue": "中国动漫博物馆", "category": "展览", "start_at": "2026-07-15T10:00:00+08:00",
    "end_at": "2026-08-31T18:00:00+08:00",
    "evidence": {"verification_status": "public_source_observed"},
}
CONCERT_REAL = {
    "id": 2, "matched_name": "谢霆锋", "title": "谢霆锋进化演唱会-杭州站",
    "venue": "杭州奥体中心体育场（大莲花）", "category": "演唱会",
    "start_at": "2026-08-09T19:00:00+08:00", "end_at": None,
    "evidence": {"verification_status": "official_source_confirmed"},
}
CONCERT_RESIDENCY = {
    "id": 3, "matched_name": "万兽之王巡回演唱会", "title": "2026薛之谦“万兽之王”巡回演唱会-杭州站",
    "venue": "杭州奥体中心体育场（大莲花）", "category": "演唱会",
    # 巡演展期覆盖周末但开始日不在周末内 → 周末场次应标 estimated（不臆测具体场次）
    "start_at": "2026-08-01T19:00:00+08:00", "end_at": "2026-08-16T22:00:00+08:00",
    "evidence": {"verification_status": "public_source_observed"},
}


# ============================ 意图识别 ============================
def test_route_design_intent_fastpath():
    assert _looks_like_route_design(USER_MSG) is True  # 既要…也要…
    assert _looks_like_route_design("帮我设计一条路线") is True
    assert _looks_like_route_design("行程怎么排") is True
    assert _looks_like_route_design("我想去看展") is False
    assert _looks_like_route_design("帮我比较高铁和飞机") is False


def test_classify_intent_offline_routes_to_design_itinerary():
    assert classify_intent(USER_MSG, use_llm=False) == "design_itinerary"


# ============================ 锚点解析 ============================
def test_resolve_anchors_matches_title_and_pending(session, make_activity):
    a1 = make_activity("2026杭州中国动漫博物馆暑期免费观影", venue="中国动漫博物馆", category="休闲")
    a2 = make_activity("2026薛之谦“万兽之王”巡回演唱会-杭州站",
                       venue="杭州奥体中心体育场", category="演唱会")
    resolved, pending = resolve_anchors(
        ["动漫博物馆", "万兽之王巡回演唱会", "不存在的神秘地点"],
        session, "310000", WS, WE)
    titles = [r["title"] for r in resolved]
    assert a1.title in titles and a2.title in titles
    assert pending == ["不存在的神秘地点"]


# ============================ 日路线求解 ============================
def test_day_route_orders_museum_day_concert_evening():
    plan = design_day_route([MUSEUM, CONCERT_REAL], [], WS, WE)
    assert plan["anchors_resolved"] == 2
    days = {d["date"]: d["slots"] for d in plan["days"]}
    # 博物馆在第一天白天
    day1 = days["2026-08-07"]
    museum_slot = next(s for s in day1 if s.get("title") == MUSEUM["title"])
    assert museum_slot["start"].endswith("T10:00:00")
    assert museum_slot["evidence"]["verification_status"] == "estimated"  # 排程时间不臆测
    # 有确切时间的演唱会归位 8/9 19:00 且透传真实证据（不降级为估算）
    day3 = days["2026-08-09"]
    concert_slot = next(s for s in day3 if s.get("title") == CONCERT_REAL["title"])
    assert concert_slot["start"].endswith("T19:00:00")
    assert concert_slot["evidence"]["verification_status"] == "official_source_confirmed"
    # 有用餐占位；不同天的锚点不生成接驳（回酒店，不跨天接驳）
    assert any(s["kind"] == "meal" for s in day1)


def test_day_route_leg_between_same_day_venues():
    other_museum = {**MUSEUM, "id": 9, "title": "杭州西湖博物馆特展", "venue": "杭州西湖博物馆"}
    plan = design_day_route([MUSEUM, other_museum], [], WS, WE)
    day1 = next(d for d in plan["days"] if d["date"] == "2026-08-07")
    legs = [s for s in day1["slots"] if s["kind"] == "leg"]
    assert legs, "同天不同场馆之间应有接驳段"
    assert "中国动漫博物馆" in legs[0]["title"] and "杭州西湖博物馆" in legs[0]["title"]
    assert legs[0]["evidence"]["verification_status"] == "estimated"


def test_day_route_residency_concert_estimated_evening():
    plan = design_day_route([MUSEUM, CONCERT_RESIDENCY], [], WS, WE)
    all_slots = [s for d in plan["days"] for s in d["slots"]]
    concert = next(s for s in all_slots if "万兽之王" in (s.get("title") or ""))
    assert concert["start"].endswith("T19:00:00")  # 默认晚场
    assert concert["evidence"]["verification_status"] == "estimated"
    assert "场次" in (concert.get("note") or "")


def test_day_route_pending_anchor_kept_as_unknown():
    plan = design_day_route([MUSEUM], ["神秘地点"], WS, WE)
    all_slots = [s for d in plan["days"] for s in d["slots"]]
    pending_slot = next(s for s in all_slots if s.get("title") == "神秘地点")
    assert pending_slot["evidence"]["verification_status"] == "unknown"
    assert "待确认" in pending_slot["note"]
    assert plan["anchors_pending"] == ["神秘地点"]
    assert any("神秘地点" in w for w in plan["warnings"])


# ============================ handle_turn 端到端（离线） ============================
def test_handle_turn_design_itinerary_end_to_end(session, make_activity):
    make_activity("2026杭州中国动漫博物馆暑期免费观影", venue="中国动漫博物馆", category="休闲")
    make_activity("2026薛之谦“万兽之王”巡回演唱会-杭州站",
                  venue="杭州奥体中心体育场（大莲花）", category="演唱会")
    ctx = {
        "target_city_name": "杭州", "target_city_code": "310000",
        "weekend_start": WS.isoformat(), "weekend_end": WE.isoformat(),
        "origins": ["上海"], "party_size": 2,
    }
    d = handle_turn("new", USER_MSG, memory_ctx=ctx, use_llm=False, session=session)
    assert d["intent"] == "design_itinerary"
    plan = d.get("route_plan")
    assert plan, "应产出 route_plan"
    titles = [s.get("title", "") for day in plan["days"] for s in day["slots"]]
    assert any("动漫博物馆" in t for t in titles)
    assert any("万兽之王" in t for t in titles)
    assert "路线" in d["reply"]


def test_handle_turn_design_itinerary_no_anchor_asks_back(session):
    ctx = {"target_city_name": "杭州", "weekend_start": WS.isoformat(),
           "weekend_end": WE.isoformat()}
    d = handle_turn("new", "帮我设计一条路线", memory_ctx=ctx, use_llm=False, session=session)
    assert d["intent"] == "design_itinerary"
    assert not d.get("route_plan")
    assert "点名" in d["reply"]


def test_design_itinerary_uses_constraints_from_message(session, make_activity):
    """消息自带约束（"下周末从杭州出发"）必须先抽取合并，再做锚点匹配——
    否则会用错城市/时间窗（实测：万兽之王被窗外城市的同名演唱会顶替）。"""
    make_activity("2026薛之谦“万兽之王”巡回演唱会-杭州站",
                  venue="杭州奥体中心体育场（大莲花）", category="演唱会",
                  city_code="330100",
                  start_at=datetime(2026, 8, 6, 16, 0, tzinfo=timezone.utc),
                  end_at=datetime(2026, 8, 15, 16, 0, tzinfo=timezone.utc))
    d = handle_turn(
        "new",
        "我既要去动漫博物馆，也要去万兽之王巡回演唱会，下周末帮我从杭州出发设计一条路线",
        memory_ctx={}, use_llm=False, session=session)
    patch = d.get("constraints_patch") or {}
    assert patch.get("origins") == ["杭州"], "消息中的出发地应抽取并持久化"
    assert patch.get("weekend_start"), "下周末应解析为具体窗口"
    plan = d.get("route_plan")
    assert plan, "应产出 route_plan"
    titles = [s.get("title", "") for day in plan["days"] for s in day["slots"]]
    assert any("万兽之王" in t for t in titles), f"锚点应在正确城市窗口匹配到：{titles}"
