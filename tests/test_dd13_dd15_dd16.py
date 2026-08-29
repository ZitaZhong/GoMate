"""DD-13/15/16 验收：最终闸/提醒/ICS、对话 Copilot、记忆覆盖语义。"""
from __future__ import annotations

import pytest

from wheretogo.copilot import classify_intent, handle_turn
from wheretogo.domain.compose import build_ics, build_reminders, run_final_gate
from wheretogo.memory import load_memory, write_memory
from wheretogo.models import User
from wheretogo.notify import dispatch
from wheretogo.orchestration.guard import ProvenanceViolation

# —— DD-13 最终闸 + 提醒 + ICS ——
def test_run_final_gate_clean_passes():
    bundle = {"activities": [{"evidence": {"source_type": "official_venue",
                "verification_status": "official_source_confirmed", "confidence": 0.9}}]}
    r = run_final_gate(bundle)
    assert r["guard_violations"] == 0 and r["fabricated_transport_count"] == 0


def test_run_final_gate_blocks_llm_confirmed():
    """KPI①：LLM 来源标为已确认 → 拦截。"""
    bad = {"activities": [{"evidence": {"source_type": "llm",
                "verification_status": "official_source_confirmed"}}]}
    with pytest.raises(ProvenanceViolation):
        run_final_gate(bad)


def test_run_final_gate_blocks_fabricated_transport():
    """KPI③：交通字段来自 LLM → 拦截。"""
    bad = {"transport": {"price": {"evidence": {"source_type": "llm"}}}}
    with pytest.raises(ProvenanceViolation):
        run_final_gate(bad)


def test_build_ics_format():
    ics = build_ics({"timeline": [{"kind": "activity", "title": "莫奈展",
                                   "start_at": "2026-07-25T10:00+00:00",
                                   "end_at": "2026-07-25T12:00+00:00", "ref_id": 1}]})
    assert "BEGIN:VCALENDAR" in ics and "BEGIN:VEVENT" in ics and "莫奈展" in ics
    assert "\r\n" in ics  # CRLF（RFC5545）


def test_build_reminders_count():
    rems = build_reminders({"plan_id": "1", "timeline": []})
    assert len(rems) == 27  # 9 类型 × 3 通道
    types = {r["type"] for r in rems}
    assert "pre_trip_72h" in types and "return_trip" in types


# —— DD-13 §3.4/§5.5 提醒 payload 与 fire_at ——
def _rich_bundle():
    return {
        "plan_id": "1",
        "time_windows": {"depart": "2026-07-24T18:30:00+08:00"},
        "transport": {
            "prefill": {"rail": {"from": "上海", "to": "北京"}},
            "presale": [{"route": "上海 → 北京", "open_at": "2026-07-10T08:00:00",
                         "disclaimer": "起售时间以 12306 当前页面为准",
                         "evidence": {"source_type": "rule", "verification_status": "estimated"}}],
            "candidates": [{"recommended_mode": "compare"}],
        },
        "activities": [],
        "bookings": [],
        "timeline": [{"kind": "activity", "title": "莫奈展",
                      "start_at": "2026-07-25T10:00:00+08:00",
                      "end_at": "2026-07-25T12:00:00+08:00"}],
    }


def test_build_reminders_payload_title_body():
    """§3.4：每类提醒都有可读 title/body（不再是空 payload）。"""
    rems = build_reminders(_rich_bundle())
    assert all(r["payload"].get("title") and r["payload"].get("body") for r in rems)


def test_build_reminders_presale_fire_at_from_transport():
    """§5.5：presale fire_at = DD-09 transport.presale.open_at，含官方入口与 disclaimer。"""
    rems = build_reminders(_rich_bundle())
    presale = next(r for r in rems if r["type"] == "presale")
    assert presale["fire_at"] == "2026-07-10T08:00:00"
    assert presale["payload"]["action_url"] == "https://www.12306.cn/"
    assert "12306" in (presale["payload"]["disclaimer"] or "")
    assert presale["payload"]["evidence"]["verification_status"] == "estimated"


def test_build_reminders_rule_based_fire_at():
    """§5.5：flight_recheck=行前5天、doc_check=行前48h、pre_trip_72h=行前72h（相对行程起点）。"""
    rems = build_reminders(_rich_bundle())
    by_type = {r["type"]: r for r in rems}
    assert by_type["flight_recheck"]["fire_at"] == "2026-07-19T18:30:00+08:00"
    assert by_type["doc_check"]["fire_at"] == "2026-07-22T18:30:00+08:00"
    assert by_type["pre_trip_72h"]["fire_at"] == "2026-07-21T18:30:00+08:00"


def test_build_reminders_flight_recheck_skipped_for_rail():
    """高铁策略不臆测飞行需求 → flight_recheck 不排时点（fire_at=None，调度跳过）。"""
    bundle = _rich_bundle()
    bundle["transport"]["candidates"] = [{"recommended_mode": "rail"}]
    fr = next(r for r in build_reminders(bundle) if r["type"] == "flight_recheck")
    assert fr["fire_at"] is None


def test_build_reminders_uncomputable_types_stay_unscheduled():
    """activity_booking/hotel_cancel_deadline 无数据源 → fire_at=None（诚实不编造时点）。"""
    by_type = {r["type"]: r for r in build_reminders(_rich_bundle())}
    assert by_type["activity_booking"]["fire_at"] is None
    assert by_type["hotel_cancel_deadline"]["fire_at"] is None


def test_reminders_preview_dedupes_and_skips_unscheduled():
    """§3.3 reminders_preview：只收会真正调度的提醒，三通道副本合并为一条。"""
    from wheretogo.domain.compose import reminders_preview
    preview = reminders_preview(build_reminders(_rich_bundle()))
    assert preview and all(p["fire_at"] for p in preview)
    keys = [(p["type"], p["title"]) for p in preview]
    assert len(keys) == len(set(keys))
    types = {p["type"] for p in preview}
    assert "presale" in types and "activity_booking" not in types


def test_compose_bundles_have_disclaimer_and_reminders_preview():
    """DD-13 §3.3：探索版/确认版均带 disclaimer 与 reminders_preview。"""
    from wheretogo.orchestration.bundle import compose_confirm_bundle, compose_explore_bundle
    state = {
        "plan_id": "1",
        "constraints": {"earliest_depart": "2026-08-08T09:00:00+08:00"},
        "transport_options": {
            "prefill": {"rail": {"from": "上海", "to": "北京"}},
            "presale": [{"route": "上海 → 北京", "open_at": "2026-07-25T08:00:00"}],
            "candidates": [{"recommended_mode": "compare"}],
        },
        "candidate_cities": [], "activities": [], "bookings": [],
    }
    eb = compose_explore_bundle(state)
    assert eb["disclaimer"]
    assert [p["type"] for p in eb["reminders_preview"]] == ["presale"]  # 探索版=起售预览（§5.1）
    cb = compose_confirm_bundle(state)
    assert cb["disclaimer"]
    cb_types = {p["type"] for p in cb["reminders_preview"]}
    assert "presale" in cb_types and "doc_check" in cb_types  # 确认版=九类摘要（§5.2）


# —— DD-13 通知通道 ——
def test_dispatch_no_key_skips_push_email_sends_ics():
    from wheretogo.config import get_settings
    assert not get_settings().vapid_public_key  # 无 key
    assert dispatch({"channel": "web_push"}) == "skipped"
    assert dispatch({"channel": "email"}) == "skipped"
    assert dispatch({"channel": "ics"}) == "sent"


# —— DD-15 对话 Copilot ——
def test_classify_intent_rules():
    assert classify_intent("我买好票了", use_llm=False) == "confirm_booking"
    assert classify_intent("帮我查最新的展", use_llm=False) == "deep_research"
    assert classify_intent("你好", use_llm=False) == "chitchat"
    assert classify_intent("随便看看", use_llm=False) == "provide_constraints"  # 兜底


def test_handle_turn_clarify_on_missing():
    d = handle_turn("p1", "想去玩", memory_ctx={}, use_llm=False)
    assert d["intent"] == "provide_constraints"
    assert d["action"] == "invoke"
    assert d["pending_clarify"]  # 缺 origins/weekend/interests → 追问


def test_handle_turn_chitchat_no_clarify():
    d = handle_turn("p1", "你好", use_llm=False)
    assert d["action"] == "answer" and d["pending_clarify"] == []


# —— DD-15 ask_info 智能问答 ——
def test_ask_info_llm_answer(session, make_activity, monkeypatch):
    """检索到候选行 → 交 LLM 结合问题作答（行内数据进 prompt，系统提示带证据红线）。"""
    from importlib import import_module
    ht = import_module("wheretogo.copilot.handle_turn")  # 包级同名函数遮蔽了子模块
    make_activity("莫奈睡莲特展", price_text="￥60")
    seen = {}

    def fake_chat(task, messages, **kwargs):
        seen["task"], seen["messages"] = task, messages
        return "莫奈睡莲特展票价￥60（官方确认），以官方页面为准。"

    monkeypatch.setattr(ht, "chat", fake_chat)
    reply = ht._answer_from_db("莫奈展门票多少钱？", session, None, use_llm=True)
    assert reply.startswith("莫奈睡莲特展")
    assert seen["task"] == "qa_answer"
    assert "只允许引用记录内的数据" in seen["messages"][0]["content"]  # 证据红线
    assert "莫奈睡莲特展" in seen["messages"][1]["content"]  # 行内数据交给 LLM


def test_ask_info_fallback_labeled(session, make_activity):
    """无 LLM → 关键词匹配拼接并显式标注降级（不静默产出）。"""
    from importlib import import_module
    ht = import_module("wheretogo.copilot.handle_turn")
    make_activity("莫奈睡莲特展", price_text="￥60")
    reply = ht._answer_from_db("莫奈展门票多少钱？", session, None, use_llm=False)
    assert "关键词匹配" in reply and "莫奈睡莲特展" in reply
    # 测试环境无 LLM key：use_llm=True 同样显式标注降级
    assert "关键词匹配" in ht._answer_from_db("莫奈展门票多少钱？", session, None, use_llm=True)


# —— DD-16 记忆覆盖语义 ——
def test_write_load_memory_with_overwrite(session):
    u = User(anon_id="u1")
    session.add(u)
    session.flush()
    write_memory(u.id, "preference", "diet", "不吃辣", session=session)
    write_memory(u.id, "preference", "diet", "能吃辣了", session=session)  # 同 key 覆盖
    mem = load_memory(u.id, session=session)
    diet = [m["content"] for m in mem["semantic"] if m["key"] == "diet"]
    assert diet == ["能吃辣了"]  # 只召回最新（旧记录 valid=FALSE）


def test_write_memory_free_recall(session):
    """无 key 自由记忆：都保留，按近期召回。"""
    u = User(anon_id="u2")
    session.add(u)
    session.flush()
    write_memory(u.id, "preference", None, "爱看印象派", session=session)
    write_memory(u.id, "preference", None, "常从上海出发", session=session)
    mem = load_memory(u.id, session=session)
    assert len(mem["semantic"]) == 2
