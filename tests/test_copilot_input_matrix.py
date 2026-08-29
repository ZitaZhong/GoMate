"""Conversation input matrix for the open-semantic Copilot contract."""
from __future__ import annotations

import pytest

from wheretogo.copilot.handle_turn import classify_intent, handle_turn
from wheretogo.copilot.nlu import extract_constraints_from_text, normalize_interests


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("你好", "chitchat"),
        ("我不想要刚才那类了", "refine_field"),
        ("再搜一批", "deep_research"),
        ("有没有别的选择", "deep_research"),
        ("车票多少钱", "ask_info"),
        ("杭州周末会下雨吗", "weather"),
        ("我买好票了", "confirm_booking"),
        ("本周末从上海去杭州", "provide_constraints"),
    ],
)
def test_offline_fallback_only_classifies_control_speech_acts(message, expected):
    assert classify_intent(message, use_llm=False) == expected


def test_offline_fallback_preserves_unknown_requirement_verbatim():
    message = "不要商业化打卡点，改找废弃矿坑摄影空间"
    result = handle_turn(
        "open",
        message,
        memory_ctx={"origins": ["上海"]},
        use_llm=False,
    )
    patch = result["constraints_patch"]
    assert patch["experience_requirements"] == [message]
    assert patch["research_goal"] == message
    assert patch["acceptance_criteria"] == [message]
    assert "interests" not in patch


def test_offline_research_feedback_is_preserved_without_topic_guessing():
    message = "再找一批更偏僻、晚上能观星的"
    result = handle_turn(
        "open",
        message,
        memory_ctx={
            "origins": ["上海"],
            "experience_requirements": ["适合两人"],
        },
        use_llm=False,
    )
    assert result["intent"] == "deep_research"
    assert result["constraints_patch"]["research_goal"] == message
    assert result["constraints_patch"]["acceptance_criteria"] == [message]
    assert result["constraints_patch"]["__research_feedback"] == message


def test_normalizers_do_not_map_open_text_to_a_domain_taxonomy():
    assert normalize_interests("看展") == ["看展"]
    assert normalize_interests(["", None, "攀岩", "攀岩"]) == ["攀岩"]


def test_stable_trip_primitives_still_have_deterministic_fallback():
    result = extract_constraints_from_text(
        "本周末从上海去杭州，2个人，预算1500，不吃辣",
        use_llm=False,
    )
    assert result["origins"] == ["上海"]
    assert result["target_city_name"] == "杭州"
    assert result["party_size"] == 2
    assert result["budget_band"] == {"max": 1500}
    assert result["dietary"] == ["辣"]
    assert result["weekend_start"]


def test_pending_short_answer_binds_to_the_asked_stable_slot():
    result = handle_turn(
        "open",
        "上海",
        memory_ctx={},
        pending_clarify_ctx=[{"slot": "origins", "q": "从哪里出发？"}],
        use_llm=False,
    )
    assert result["constraints_patch"]["origins"] == ["上海"]


@pytest.mark.parametrize(
    "message",
    [
        "？？？？",
        "🎵🎨🚄",
        "随便，你决定",
        "show me something entirely new in Hangzhou",
        "<img src=x onerror=alert(1)>",
        "'; DROP TABLE plans; --",
    ],
)
def test_unusual_inputs_never_break_the_turn_contract(message):
    result = handle_turn(
        "matrix",
        message,
        memory_ctx={"origins": ["上海"]},
        use_llm=False,
    )
    assert isinstance(result["reply"], str) and result["reply"]
    assert isinstance(result["acts"], list)
    assert isinstance(result["commands"], list)
