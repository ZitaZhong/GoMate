"""DD-02 编排层验收测试（对应 DD-02 §13 DoD）。

用内存 checkpointer 的 PlannerService；research/dining 走真实 DD-05 检索（读隔离库）。
覆盖：两段式 interrupt/resume、跨天恢复不重算、可回退/重规划、降级、Guard 拦截、条件边路由。
"""
from __future__ import annotations

import pytest

from wheretogo.orchestration import PlannerService, ProvenanceViolation, route_after_validate
from wheretogo.orchestration.guard import assert_guard, run_guard

SH = {"query": "周末 展览", "interests": ["展览"], "target_city_code": "310000"}
_BOOKING = [
    {
        "kind": "hotel",
        "extracted": {"name": "示例酒店", "area": "人民广场"},
        "confirmed": True,
        "evidence": {"source_type": "user_provided", "verification_status": "confirmed_by_user", "confidence": 1.0},
    }
]


# ---------------- 纯函数 ----------------
def test_route_after_validate():
    assert route_after_validate({"validation": {"ok": True}}) == "ok"
    assert route_after_validate({"validation": {"ok": False, "issues": ["RETURN_TIGHT"]}}) == "retransport"
    assert route_after_validate({"validation": {"ok": False, "issues": ["OTHER"]}}) == "reflow"


def test_guard_blocks_llm_marked_confirmed():
    bad = {"activities": [{"title": "x", "evidence": {
        "source_type": "llm", "verification_status": "official_source_confirmed", "confidence": 0.9}}]}
    assert run_guard(bad), "LLM 来源标为官方确认必须被判违规"
    with pytest.raises(ProvenanceViolation):
        assert_guard(bad)


def test_guard_passes_clean_and_uncertain():
    clean = {"activities": [
        {"evidence": {"source_type": "official_venue", "verification_status": "official_source_confirmed", "confidence": 0.9}},
        {"evidence": {"source_type": "llm", "verification_status": "estimated", "confidence": 0.4}},
        {"evidence": {"source_type": "user_provided", "verification_status": "confirmed_by_user", "confidence": 1.0}},
    ]}
    assert run_guard(clean) == []


# ---------------- 两段式端到端 ----------------
def test_two_phase_interrupt_then_resume():
    svc = PlannerService()
    r1 = svc.start("e2e-1", SH)
    assert r1["interrupt"] is not None
    assert r1["interrupt"]["type"] == "await_booking"
    assert r1["interrupt"]["explore_bundle"]["version"] == "explore"

    r2 = svc.resume("e2e-1", _BOOKING)
    st = r2["state"]
    assert st["stage"] == "confirm"
    assert st["bundle"]["version"] == "confirm"
    assert st["bookings"] == _BOOKING
    assert r2["interrupt"] is None  # 已跑到 END，无挂起中断


def test_resume_does_not_recompute_explore_products():
    """跨天恢复：explore 阶段产物（候选城市/活动）在 resume 后与中断时一致（checkpoint 恢复）。"""
    svc = PlannerService()
    svc.start("e2e-2", SH)
    before = svc.get_state("e2e-2").values
    cities_before = before.get("candidate_cities")
    acts_before = before.get("activities")

    svc.resume("e2e-2", _BOOKING)
    after = svc.get_state("e2e-2").values
    assert after.get("candidate_cities") == cities_before  # 未重算
    assert after.get("activities") == acts_before


def test_confirm_bundle_passes_guard():
    """确认版出稿必然通过 Provenance Guard（compose 未抛异常即证明）。"""
    svc = PlannerService()
    svc.start("e2e-3", SH)
    st = svc.resume("e2e-3", _BOOKING)["state"]
    assert run_guard(st["bundle"]) == []


# ---------------- 可回退 / 重规划 ----------------
def test_replan_from_dining_preserves_bookings():
    svc = PlannerService()
    svc.start("e2e-4", SH)
    svc.resume("e2e-4", _BOOKING)
    out = svc.replan("e2e-4", reason="weather", from_node="dining")
    st = out["state"]
    assert st.get("replan_reason") == "weather"
    assert st["bookings"] == _BOOKING  # 已确认回填保留
    assert st["bundle"]["version"] == "confirm"


def test_revise_updates_state_and_recomposes():
    svc = PlannerService()
    svc.start("e2e-5", SH)
    svc.resume("e2e-5", _BOOKING)
    changed = {**SH, "note": "已改约束"}
    out = svc.revise("e2e-5", {"constraints": changed}, from_node="timeline")
    st = out["state"]
    assert st["constraints"].get("note") == "已改约束"
    assert st["bookings"] == _BOOKING


# ---------------- 降级 ----------------
def test_degrade_empty_activities_still_completes():
    """检索空（该城无活动数据）→ 带 warning 继续，仍产出确认版 bundle（§10 韧性）。"""
    svc = PlannerService()
    empty_city = {"query": "周末", "target_city_code": "120000"}  # 天津：有城市档案、无样例活动
    r1 = svc.start("e2e-6", empty_city)
    assert r1["interrupt"] is not None
    st = svc.resume("e2e-6", _BOOKING)["state"]
    assert st["bundle"]["version"] == "confirm"
    assert st["bundle"]["activities"] == []
    assert any("活动检索为空" in w for w in st.get("warnings", []))
