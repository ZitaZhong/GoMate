"""v3 回归修复单测（离线确定性；对齐 回归测试报告_2026-07-23）。

覆盖：P0-3 假数据消除、N1 脏数据校验、C3/C4/C5/C6 对话、decide_mode、天气重规划、§06 bundle、
提醒投递诚实性。均不依赖外部 key（conftest 强制离线）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wheretogo.copilot.handle_turn import _qa_keywords, _summarize, classify_intent, handle_turn
from wheretogo.copilot.nlu import extract_constraints_from_text
from wheretogo.domain.backfill import local_parse_booking
from wheretogo.domain.compose import build_reminders
from wheretogo.domain.stay_mobility_dining import _collect_dining_pois, plan_local_mobility
from wheretogo.domain.timeline import solve_timeline, validate_timeline
from wheretogo.domain.transport import decide_mode
from wheretogo.notify.channels import dispatch
from wheretogo.notify.reminders import persist_reminders
from wheretogo.orchestration.bundle import _overlay_primary_count, compose_confirm_bundle, compose_explore_bundle
from wheretogo.orchestration.nodes import _in_window, weather_awareness
from wheretogo.retrieval import Weekend

_EV = {"source_type": "official_venue", "verification_status": "official_source_confirmed"}


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ========================= P0-3 假数据消除 =========================
def test_dining_no_fake_data_when_no_source():
    """无 amap/search key → 空态（诚实），绝不返回写死的假餐厅。"""
    assert _collect_dining_pois("310000", "上海", "市中心") == []


def test_mobility_no_arithmetic_sequence():
    """接驳不再用 20+i*5：无坐标→minutes=None；有坐标→真实几何估算。"""
    legs = plan_local_mobility(
        [{"title": "A", "location": None}, {"title": "B", "location": None}],
        {"name": "住宿区", "center": None},
    )
    assert legs and all(leg["minutes"] is None for leg in legs)
    legs2 = plan_local_mobility(
        [{"title": "展馆", "location": [121.49, 31.24]}],
        {"name": "住宿区", "center": {"lng": 121.47, "lat": 31.23}},
    )
    assert legs2[0]["minutes"] is not None  # 高德 route 无 key→直线估算（真实几何，非写死）


# ========================= N1 脏数据校验（倒序/过期） =========================
def test_validate_flags_inverted_range():
    slots = [{"kind": "activity", "title": "坏数据",
              "start_at": "2026-07-26T08:30:00+08:00", "end_at": "2026-07-23T09:30:00+08:00"}]
    v = validate_timeline(slots, {})
    assert v["ok"] is False and "INVERTED_RANGE" in v["issues"]


def test_validate_flags_expired_activity():
    slots = [{"kind": "activity", "title": "过期",
              "start_at": "2020-01-01T10:00:00+08:00", "end_at": "2020-01-01T12:00:00+08:00"}]
    assert "EXPIRED_ACTIVITY" in validate_timeline(slots, {})["issues"]


def test_solve_skips_inverted_and_expired():
    future = datetime.now(timezone.utc) + timedelta(days=3)
    good = {"title": "好活动", "id": 1, "evidence": _EV,
            "start_at": _iso(future), "end_at": _iso(future + timedelta(hours=2))}
    inverted = {"title": "倒序", "id": 2, "evidence": _EV,
                "start_at": _iso(future + timedelta(hours=5)), "end_at": _iso(future + timedelta(hours=4))}
    expired = {"title": "过期", "id": 3, "evidence": _EV,
               "start_at": "2020-01-01T10:00:00+08:00", "end_at": "2020-01-01T12:00:00+08:00"}
    titles = [s["title"] for s in solve_timeline([good, inverted, expired], [], [], {})
              if s.get("kind") == "activity"]
    assert "好活动" in titles and "倒序" not in titles and "过期" not in titles


# ========================= C3/C4/C5/C6 对话 =========================
def test_c3_destination_change_not_origin():
    """'目的地换成西安' → 抽为目的地，不误当出发地。"""
    out = extract_constraints_from_text("目的地换成西安吧", use_llm=False)
    assert out.get("target_city_name") == "西安"
    assert "西安" not in (out.get("origins") or [])


def test_c4_qa_keywords_extracts_entity():
    """问答关键词兜底能抽出实体（如'莫奈'），不再整句拼接截断。"""
    kws = _qa_keywords("莫奈那个展在哪儿办？门票多少？", use_llm=False)
    assert any("莫奈" in k for k in kws)


def test_c5_booking_intent_and_hotel_name():
    """'酒店我订好了…店' → confirm_booking，且酒店名（…店后缀）可抽取。"""
    assert classify_intent("酒店我订好了，全季上海人民广场店，这周五入住", use_llm=False) == "confirm_booking"
    draft = local_parse_booking("酒店我订好了，全季上海人民广场店，这周五入住")
    assert draft["kind"] == "hotel" and "店" in (draft["extracted"].get("name") or "")


def test_c6_weather_intent_recognized():
    assert classify_intent("周日好像要下暴雨，行程要不要调整？", use_llm=False) == "weather"


# ========================= decide_mode 距离带（P1-1） =========================
def test_decide_mode_distance_bands():
    assert decide_mode({"total_min": 120}, {"total_min": 200}, 150) == "rail"      # 中短途偏铁
    assert decide_mode({"total_min": 400}, {"total_min": 200}, 1200) == "air"       # 长途航班更快
    assert decide_mode({"total_min": 190}, {"total_min": 200}, 500) == "compare"    # 相近→并列比较


# ========================= 天气重规划（P1-7/C6） =========================
def test_weather_awareness_user_declared():
    out = weather_awareness({"replan_reason": "weather", "candidate_cities": []})
    assert out["weather"]["adverse"] is True and out.get("warnings")


def test_weather_reorders_indoor_first():
    future = datetime.now(timezone.utc) + timedelta(days=3)
    outdoor = {"title": "户外市集", "category": "市集", "id": 1, "evidence": _EV,
               "start_at": _iso(future), "end_at": _iso(future + timedelta(hours=2))}
    indoor = {"title": "莫奈展", "category": "展览", "id": 2, "evidence": _EV,
              "start_at": _iso(future), "end_at": _iso(future + timedelta(hours=2))}  # 同时段→冲突
    normal = [s["title"] for s in solve_timeline([outdoor, indoor], [], [], {}) if s.get("kind") == "activity"]
    stormy = [s["title"] for s in solve_timeline([outdoor, indoor], [], [], {}, weather={"adverse": True})
              if s.get("kind") == "activity"]
    assert "莫奈展" in stormy and stormy != normal  # 恶劣天气室内优先，重规划改变行为


# ========================= §06 Bundle 字段 =========================
def test_bundle_has_prd_section6_fields():
    state = {
        "plan_id": "1",
        "constraints": {"interests": ["展览", "美食"], "budget_band": {"max": 1500},
                        "earliest_depart": "2026-08-08T09:00:00+08:00",
                        "latest_return": "2026-08-10T21:00:00+08:00"},
        "candidate_cities": [{"name": "上海", "city_code": "310000", "risks": {"value": {"note": "梅雨"}}}],
        "activities": [], "bookings": [],
    }
    eb = compose_explore_bundle(state)
    assert eb["theme"] and eb["budget_range"]["max"] == 1500
    assert eb["pending_checklist"] and "time_windows" in eb and "lodging_area" in eb
    cb = compose_confirm_bundle(state)
    assert "cost" in cb and "risks" in cb and "alternatives" in cb


# ========================= 提醒投递诚实性（P1-8） =========================
def test_reminders_build_and_dispatch_honesty():
    rem = build_reminders({"plan_id": "1", "timeline": [
        {"kind": "activity", "title": "展", "start_at": "2026-08-08T10:00:00+08:00",
         "end_at": "2026-08-08T12:00:00+08:00"}]})
    assert rem  # 生成提醒规格
    assert dispatch({"channel": "ics", "type": "pre_trip_72h", "payload": {}}) == "sent"
    assert dispatch({"channel": "web_push", "type": "pre_trip_72h", "payload": {}}) == "skipped"  # 无 VAPID key


def test_persist_reminders_rejects_non_digit_plan():
    assert persist_reminders("notadigit", [{"type": "presale", "channel": "ics",
                                            "fire_at": "2026-08-08T10:00:00+08:00"}]) == 0


# ========================= 对话多轮记忆 / 本周末解析（截图回归） =========================
def test_nlu_resolves_relative_weekend_to_dates():
    """“本周末”→ 具体 weekend_start/end（不再只置 hint，修不被接受）。"""
    out = extract_constraints_from_text("本周末去玩", use_llm=False)
    assert out.get("weekend_start") and out.get("weekend_end")


def test_c3b_go_destination_and_origin():
    """稳定地点被解析；开放体验原文保存在 research_goal。"""
    out = extract_constraints_from_text("我从上海出发去杭州看演唱会", use_llm=False)
    assert out.get("origins") == ["上海"] and out.get("target_city_name") == "杭州"
    assert "interests" not in out


def test_summarize_includes_weekend_and_never_empty():
    s = _summarize({"weekend_start": "2026-07-25T00:00:00+08:00"})
    assert "周末" in s and "07-25" in s


def test_multiturn_memory_no_reask_origin():
    """第一轮给出发地→第二轮“本周末”：合并后不再追问出发地，且关键约束齐备。"""
    d1 = handle_turn("new", "我从上海出发看演唱会", memory_ctx={}, use_llm=False, session=None)
    ctx = dict(d1.get("constraints_patch") or {})
    assert "上海" in (ctx.get("origins") or [])  # 第一轮记住出发地
    d2 = handle_turn("new", "本周末", memory_ctx=ctx, use_llm=False, session=None)
    slots = [c["slot"] for c in d2["pending_clarify"]]
    assert "origins" not in slots  # 不再重复追问已知出发地
    assert not d2["pending_clarify"]  # 出发地/周末/兴趣均齐备


# ========================= 裸城市回答的上下文纠偏（用户实测回归） =========================
def test_bare_city_answer_fills_origin_when_pending():
    """目的地已定、追问出发地后，裸城市回答（"上海"）填 origins 而非改目的地。"""
    ctx = {
        "target_city_name": "杭州", "target_city_code": "330100",
        "weekend_start": "2026-08-15T00:00:00+08:00",
        "weekend_end": "2026-08-16T23:59:59+08:00",
    }
    d = handle_turn("new", "上海", memory_ctx=ctx, use_llm=False, session=None)
    patch = d.get("constraints_patch") or {}
    assert patch.get("origins") == ["上海"], f"裸城市应填出发地：{patch}"
    assert "target_city_name" not in patch, "不应覆盖已有目的地"
    assert not d["pending_clarify"], "槽位齐备后不应再重复追问"


def test_bare_city_first_message_sets_target():
    """首轮裸城市（无上下文）按目的地理解，不触发纠偏。"""
    d = handle_turn("new", "杭州", memory_ctx={}, use_llm=False, session=None)
    patch = d.get("constraints_patch") or {}
    assert patch.get("target_city_name") == "杭州"
    assert not patch.get("origins")


def test_explicit_target_change_not_remapped():
    """带指令词的"改去上海"是显式改目的地，不被出发地纠偏劫持。"""
    ctx = {
        "target_city_name": "杭州", "target_city_code": "330100",
        "weekend_start": "2026-08-15T00:00:00+08:00",
        "weekend_end": "2026-08-16T23:59:59+08:00",
    }
    d = handle_turn("new", "改去上海", memory_ctx=ctx, use_llm=False, session=None)
    patch = d.get("constraints_patch") or {}
    assert patch.get("target_city_name") == "上海"
    assert not patch.get("origins")


def test_llm_prompt_carries_conversation_context():
    """抽取 prompt 注入已记下约束与未决问题（修"只传上海两个字"）：缺出发地时带追问提示。"""
    from wheretogo.copilot.nlu import _context_block
    ctx = {"target_city_name": "杭州", "weekend_start": "2026-08-15T00:00:00+08:00",
           "weekend_end": "2026-08-16T23:59:59+08:00", "interests": ["看展"]}
    block = _context_block(ctx)
    assert "目的地=杭州" in block and "出发地=未填写" in block
    assert "填入 origins" in block  # 缺 origins → 明确提示模型填出发地
    assert _context_block({}) == ""  # 空记忆不注入
    ctx_full = {**ctx, "origins": ["上海"]}
    assert "填入 origins" not in _context_block(ctx_full)  # 槽位已齐则无追问提示


def test_llm_prompt_carries_recent_history():
    """近 N 轮对话注入抽取 prompt（DD-15 重做：NLU 不再无状态）。"""
    from wheretogo.copilot.nlu import _context_block
    history = [
        {"role": "user", "content": "下下周末去杭州"},
        {"role": "assistant", "content": "已记下：目的地改杭州。你们从哪里出发？"},
    ]
    block = _context_block({"target_city_name": "杭州"}, history)
    assert "近期对话" in block and "下下周末去杭州" in block
    assert "你们从哪里出发" in block


def test_question_intent_not_hijacked_by_constraints():
    """疑问句含出行约束时不被纠偏为 provide_constraints（修"门票多少钱"被"已记下"吞掉）。"""
    from wheretogo.copilot.handle_turn import _looks_like_question
    assert _looks_like_question("万兽之王演唱会门票多少钱") is True
    assert _looks_like_question("这个展在哪里") is True
    assert _looks_like_question("从上海出发想去杭州看演唱会") is False
    d = handle_turn("new", "万兽之王演唱会门票多少钱",
                    memory_ctx={"target_city_name": "杭州"}, use_llm=False, session=None)
    assert d["intent"] == "ask_info", f"疑问句应留在 ask_info：{d['intent']}"
    assert "多少钱" not in (d.get("constraints_patch") or {}), "不应产生约束补丁"
    # 硬约束挂问句尾巴：仍优先约束捕获（不回归 test_intent_override 场景）
    d2 = handle_turn("new", "从上海出发想看展览，门票多少",
                     memory_ctx={}, use_llm=False, session=None)
    assert d2["intent"] == "provide_constraints"
    assert "上海" in ((d2.get("constraints_patch") or {}).get("origins") or [])


def test_conversation_persists_across_turns():
    """plans.conversation 落库：第二轮后应有 4 条（2 用户 + 2 AI），供 NLU 历史注入。"""
    from fastapi.testclient import TestClient
    from wheretogo.bff.app import app
    from wheretogo.db import get_session
    from wheretogo.models import Plan
    c = TestClient(app)
    r1 = c.post("/plans/new/chat", json={"message": "我从上海出发想去杭州看展"})
    assert r1.status_code == 200
    pid = r1.json()["plan_id"]
    r2 = c.post(f"/plans/{pid}/chat", json={"message": "下周末"})
    assert r2.status_code == 200
    try:
        with get_session() as s:
            p = s.get(Plan, int(pid))
            conv = p.conversation or []
            assert len(conv) == 4, f"应存 2 轮 4 条，实际 {len(conv)}"
            assert conv[0]["role"] == "user" and conv[1]["role"] == "assistant"
            assert conv[2]["content"] == "下周末"
    finally:
        with get_session() as s:
            s.query(Plan).filter_by(id=int(pid)).delete()
            s.commit()


def test_nlu_relative_day_resolves():
    """“明天”等相对日期与地点仍由稳定属性解析器抽取。"""
    out = extract_constraints_from_text("明天从上海去杭州看演唱会", use_llm=False)
    assert out.get("weekend_start") and out.get("weekend_end")
    assert out.get("origins") == ["上海"] and out.get("target_city_name") == "杭州"
    assert "interests" not in out


def test_intent_override_captures_constraints_even_if_askinfo():
    """带问词的约束消息（被规则归为 ask_info）→ 纠偏为 provide_constraints 并保住约束。"""
    d = handle_turn("new", "从上海出发想看展览，门票多少", memory_ctx={}, use_llm=False, session=None)
    assert d["intent"] == "provide_constraints"
    assert "上海" in ((d.get("constraints_patch") or {}).get("origins") or [])


# ========================= 活动出行时段准确性 / 城市卡计数（截图回归） =========================
def test_in_window_filters_out_of_range_activities():
    wk = Weekend(datetime(2026, 7, 25, tzinfo=timezone.utc), datetime(2026, 7, 27, tzinfo=timezone.utc))
    assert _in_window("2026-07-26T10:00:00+00:00", wk) is True
    assert _in_window("2026-08-15T10:00:00+00:00", wk) is False  # 超出出行窗口→不推荐
    assert _in_window(None, wk) is False  # 无日期→不推荐


def test_overlay_primary_count_fixes_stale_zero():
    """主城卡计数用实际入窗活动数覆盖（消除 discover 早于深研的 0 场）。"""
    cities = [{"name": "杭州", "driven_by_activities": {"value": 0}, "reason": "城市档案匹配；活动待补搜"}]
    out = _overlay_primary_count(cities, [{"title": "a"}, {"title": "b"}])
    assert out[0]["driven_by_activities"]["value"] == 2
    assert "2 场" in out[0]["reason"]
    assert cities[0]["driven_by_activities"]["value"] == 0  # 不改原对象


def test_overlay_primary_count_updates_nonzero_reason_and_evidence_note():
    cities = [{
        "name": "杭州",
        "reason": "当周 7 场可选活动，门到门约 119 分钟",
        "driven_by_activities": {
            "value": 7,
            "evidence": {"note": "当周活动数 7"},
        },
    }]
    out = _overlay_primary_count(cities, [{"title": f"活动{i}"} for i in range(6)])
    assert out[0]["driven_by_activities"]["value"] == 6
    assert "当周 6 场" in out[0]["reason"]
    assert out[0]["driven_by_activities"]["evidence"]["note"] == "最终入窗可信活动数 6"
    assert cities[0]["driven_by_activities"]["value"] == 7
