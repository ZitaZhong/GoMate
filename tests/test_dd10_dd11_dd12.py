"""DD-10/11/12 验收：回填抽取/确认、住宿·接驳·餐饮、时间线求解与硬约束校验。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wheretogo.domain import (
    build_resume_payload,
    confirm_booking,
    plan_hotel_area,
    plan_local_mobility,
    run_extract,
    solve_timeline,
    to_timeline_anchors,
    validate_timeline,
)
from wheretogo.orchestration.guard import run_guard


# —— DD-10 回填 ——
def test_run_extract_manual_returns_empty_draft():
    d = run_extract("train", "manual", None)
    assert d["extracted"] == {} and d["ready_for_resume"] is False


def test_confirm_booking_ready_and_guard_safe():
    draft = {"kind": "train", "input_kind": "manual", "extracted": {"from_station": "上海"}}
    # 缺关键字段 → not ready
    b1 = confirm_booking(draft)
    assert b1["confirmed"] is False
    # 补齐 → ready，evidence = confirmed_by_user + user_provided（Guard 唯一合法路径）
    b2 = confirm_booking(draft, {"to_station": "北京", "date": "2026-08-09"})
    assert b2["confirmed"] is True
    assert b2["evidence"]["verification_status"] == "confirmed_by_user"
    assert b2["evidence"]["source_type"] == "user_provided"
    assert run_guard({"bundle": [b2]}) == []  # 不被 Guard 拦截


def test_to_timeline_anchors_by_kind():
    anchors = to_timeline_anchors([
        {"kind": "train", "extracted": {"from_station": "上海", "to_station": "北京"}, "evidence": {}},
        {"kind": "hotel", "extracted": {"name": "示例酒店"}, "evidence": {}},
    ])
    kinds = [a["kind"] for a in anchors]
    assert kinds == ["transport", "lodging"]
    assert all(a["ref_table"] == "bookings" for a in anchors)


def test_build_resume_payload_passthrough():
    bk = [{"kind": "hotel"}]
    assert build_resume_payload(bk) == bk


# —— DD-11 住宿/接驳/餐饮 ——
def test_plan_hotel_area_prefers_booking():
    state = {"bookings": [{"kind": "hotel", "extracted": {"name": "X酒店"},
                           "evidence": {"source_type": "user_provided", "verification_status": "confirmed_by_user"}}]}
    out = plan_hotel_area(state, session=None)
    assert out["source"] == "booking" and out["detail"]["name"] == "X酒店"


def test_plan_local_mobility_estimated():
    legs = plan_local_mobility([{"title": "A"}, {"title": "B"}], {"name": "酒店"})
    assert len(legs) == 2
    assert all(leg["evidence"]["verification_status"] == "estimated" for leg in legs)


# —— DD-12 时间线求解（冲突感知）——
def _act(title, start, end=None):
    return {"title": title, "start_at": start.isoformat(),
            "end_at": (end or start + timedelta(hours=2)).isoformat(),
            "id": 1, "evidence": {"source_type": "official_venue", "verification_status": "official_source_confirmed"}}


def test_solve_timeline_drops_overlapping_activities():
    base = (datetime.now(timezone.utc) + timedelta(days=7)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    # A: 10-12, B: 11-13（与 A 重叠）→ B 应被跳过；C: 13-15（不重叠）→ 保留
    acts = [_act("A", base, base + timedelta(hours=2)),
            _act("B", base + timedelta(hours=1), base + timedelta(hours=3)),
            _act("C", base + timedelta(hours=3), base + timedelta(hours=5))]
    slots = solve_timeline(acts, [], [], {})
    titles = [s["title"] for s in slots if s["kind"] == "activity"]
    assert "A" in titles and "C" in titles and "B" not in titles
    # 出稿无重叠（硬冲突率=0）
    v = validate_timeline(slots, {})
    assert "HARD_CONFLICT" not in (v["issues"] or [])


def test_solve_timeline_inserts_meal_and_buffer():
    base = (datetime.now(timezone.utc) + timedelta(days=7)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    acts = [_act("A", base, base + timedelta(hours=2)),
            _act("C", base + timedelta(hours=3), base + timedelta(hours=5))]
    slots = solve_timeline(acts, [{"name": "本帮菜馆", "evidence": {}}], [], {})
    kinds = [s["kind"] for s in slots]
    assert "meal" in kinds and kinds[-1] == "buffer"


# —— DD-12 校验 ——
def test_validate_detects_hard_conflict():
    slots = [{"seq": 0, "start_at": "2026-07-25T10:00+00:00", "end_at": "2026-07-25T12:00+00:00"},
             {"seq": 1, "start_at": "2026-07-25T11:00+00:00", "end_at": "2026-07-25T13:00+00:00"}]
    v = validate_timeline(slots, {})
    assert "HARD_CONFLICT" in v["issues"]
    assert v["metrics"]["hard_conflict"] is True


def test_validate_return_tight():
    slots = [{"seq": 0, "start_at": "2026-07-25T10:00+00:00", "end_at": "2026-07-27T22:00+00:00"}]
    v = validate_timeline(slots, {"latest_return": "2026-07-27T18:00+00:00"})
    assert "RETURN_TIGHT" in v["issues"]


def test_validate_clean_ok():
    slots = [{"seq": 0, "start_at": "2026-07-25T10:00+00:00", "end_at": "2026-07-25T12:00+00:00"},
             {"seq": 1, "start_at": "2026-07-25T13:00+00:00", "end_at": "2026-07-25T15:00+00:00"}]
    v = validate_timeline(slots, {})
    assert v["ok"] is True and v["issues"] == []
