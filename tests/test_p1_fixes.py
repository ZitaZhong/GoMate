"""P1 修复回归：本地正则回填 / Copilot 最小闭环 / 多城候选 / 官方源清单降级。"""
from __future__ import annotations

from wheretogo.copilot import handle_turn
from wheretogo.domain.backfill import local_parse_booking, run_extract
from wheretogo.domain.destination import destination_discovery


# —— P1-3 本地正则回填（无 LLM key 也能抽）——
def test_local_parse_train():
    d = local_parse_booking("我买好票了，G7502次周六早上8点上海虹桥到杭州东，二等座73元")
    assert d["kind"] == "train"
    ex = d["extracted"]
    assert ex["train_no"] == "G7502"
    assert ex["from_station"] == "上海虹桥"  # 不被 "8点" 污染
    assert ex["to_station"] == "杭州东"
    assert ex["dep_time"] == "8:00"


def test_local_parse_flight():
    d = local_parse_booking("航班 CA1501 上海浦东到北京首都 9:30 12:40 2026-08-09")
    assert d["kind"] == "flight"
    assert d["extracted"]["flight_no"] == "CA1501"
    assert d["extracted"]["dep_airport"] == "上海浦东"


def test_local_parse_hotel():
    d = local_parse_booking("订了全季酒店人民广场店，入住2026-08-08")
    assert d["kind"] == "hotel"
    assert "全季酒店" in d["extracted"]["name"]


def test_run_extract_falls_back_to_local_without_llm():
    d = run_extract("manual", "text", "G7502 上海虹桥到杭州东 8:00")
    assert d["source"] == "local_regex"  # 无 LLM key → 本地正则兜底
    assert d["extracted"]["train_no"] == "G7502"


# —— C2 Copilot 最小闭环（报告 S1/S5/S6）——
def test_copilot_extracts_constraints_from_s1():
    msg = "我们两个人这周五下班后从上海出发，周日晚上回来，预算人均1500，想看展览吃美食，完全不吃辣"
    d = handle_turn("p", msg, memory_ctx={}, use_llm=False)
    assert d["intent"] == "provide_constraints"
    patch = d["constraints_patch"] or {}
    assert "上海" in (patch.get("origins") or [])
    assert patch.get("party_size") == 2
    assert patch.get("budget_band", {}).get("max") == 1500
    assert patch.get("research_goal") == msg
    assert "interests" not in patch
    assert "辣" in (patch.get("dietary") or [])
    assert d["reply"]  # 有自然语言回复


def test_copilot_confirm_booking_extracts_s5():
    msg = "我买好票了，G7502次周六早上8点上海虹桥到杭州东，二等座73元"
    d = handle_turn("p", msg, memory_ctx={}, use_llm=False)
    assert d["intent"] == "confirm_booking"
    assert d["booking"] is not None
    ex = d["booking"]["extracted"]
    assert ex["train_no"] == "G7502"
    assert ex["from_station"] == "上海虹桥"


def test_copilot_chitchat_replies():
    d = handle_turn("p", "你好呀", use_llm=False)
    assert d["intent"] == "chitchat"
    assert d["reply"]  # 不再无回应


def test_copilot_does_not_reask_known_slots():
    """memory_ctx 已含 origins → 不再追问出发地（修报告 S1 反复追问）。"""
    d = handle_turn("p", "想看展览", memory_ctx={"origins": ["上海"], "interests": ["展览"]}, use_llm=False)
    slots = [c["slot"] for c in d["pending_clarify"]]
    assert "origins" not in slots  # 已知不追问


# —— P1-1 多城候选（PRD §04 3 城市）——
def test_destination_returns_up_to_three_cities(session):
    state = {"constraints": {"target_city_code": "330100",  # 杭州
                             "weekend_start": "2026-07-25", "weekend_end": "2026-07-27"}}
    out = destination_discovery(state, session)
    cards = out["candidate_cities"]
    assert 1 <= len(cards) <= 3
    assert cards[0]["city_code"] == "330100"  # 用户目标置首
    assert out["origin"]["city_code"] == "310000"  # 默认上海出发


def test_destination_excludes_origin_city(session):
    """出发地上海不应作为目的地候选。"""
    state = {"constraints": {"target_city_code": "330100",
                             "weekend_start": "2026-07-25", "weekend_end": "2026-07-27"}}
    out = destination_discovery(state, session)
    codes = [c["city_code"] for c in out["candidate_cities"]]
    assert "310000" not in codes  # 上海(出发地)不在候选
