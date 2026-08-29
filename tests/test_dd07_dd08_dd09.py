"""DD-07/08/09 验收：约束/目的地发现/交通决策（领域纯函数 + discover 集成）。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from wheretogo.domain import (
    BUFFER_RULES_MIN,
    aggregate_party,
    build_12306_entry,
    build_transport_options,
    door_to_door,
    estimate_door_to_door,
    intersect_bands,
    missing_slots,
    parse_constraints,
    presale_open_time,
    score_city,
)
from wheretogo.domain.destination import destination_discovery
from wheretogo.schemas import build_rerank_query

# —— DD-07 约束 ——
def test_parse_constraints_applies_defaults_and_query():
    c, warnings = parse_constraints({"interests": ["展览"], "target_city_code": "310000"})
    assert c["query"]  # 自动构造检索 query
    assert c["target_city_code"] == "310000"
    assert c["party_size"] == 1
    assert any("出发地" in w for w in warnings)  # origins 缺失


def test_parse_constraints_preserves_explicit_query():
    c, _ = parse_constraints({"query": "周末 展览", "target_city_code": "120000"})
    assert c["query"] == "周末 展览"
    assert c["target_city_code"] == "120000"


def test_missing_slots():
    # 只有 origins 是必填槽位，weekend/interests 默认智能填充
    assert "origins" in missing_slots({})
    assert "weekend" not in missing_slots({})  # weekend 不再是必填
    full = {"origins": ["上海"]}
    assert missing_slots(full) == []
    # 有 origins 就够了，即使无 weekend/interests
    assert missing_slots({"origins": ["杭州"]}) == []


def test_intersect_bands_conflict():
    assert intersect_bands({"min": 100, "max": 200}, {"min": 300, "max": 400})["_conflict"] is True
    assert intersect_bands({"min": 100, "max": 300}, {"min": 200, "max": 400}) == {"min": 200, "max": 300}


def test_aggregate_party_fairness():
    agg = aggregate_party([
        {"earliest_depart": "2026-07-25T08:00", "latest_return": "2026-07-27T20:00",
         "budget_band": {"min": 500, "max": 1500}, "accept_flight": True, "accept_night_train": False,
         "interests": ["展览"], "dietary": ["不吃辣"]},
        {"earliest_depart": "2026-07-25T06:00", "latest_return": "2026-07-27T18:00",
         "budget_band": {"min": 800, "max": 2000}, "accept_flight": False, "accept_night_train": True,
         "interests": ["演出"], "dietary": []},
    ])
    assert agg["earliest_depart"] == "2026-07-25T08:00"  # max（各人最晚能走）
    assert agg["latest_return"] == "2026-07-27T18:00"    # min（各人最早要回）
    assert agg["budget_band"]["min"] == 800 and agg["budget_band"]["max"] == 1500  # 交集
    assert agg["accept_flight"] is False and agg["accept_night_train"] is False    # all
    assert set(agg["interests"]) == {"展览", "演出"}  # 并集


# —— DD-09 交通 ——
def test_buffer_constants():
    assert BUFFER_RULES_MIN["rail"] == {"ingress": 25, "egress": 15}
    assert BUFFER_RULES_MIN["air"]["checkin_with_bag"] == 90


def test_presale_open_time():
    # 乘车日 2026-08-09，起售 14 天前同时点
    assert presale_open_time(date(2026, 8, 9), "08:00") == datetime(2026, 7, 26, 8, 0)


def test_build_12306_entry_no_params_buy():
    e = build_12306_entry("上海", "北京", date(2026, 8, 9))
    assert "12306.cn" in e["url"]
    assert e["prefill_hint"]["date"] == "2026-08-09"
    assert "不代购" in e["disclaimer"]


def test_transport_same_city_local():
    opts = build_transport_options({"origins": [], "target_city_code": "310000"}, [])
    assert opts["candidates"][0]["recommended_mode"] == "local"
    assert opts["prefill"] == {} and opts["presale"] == []


def test_transport_intercity_has_prefill_presale():
    opts = build_transport_options(
        {"origins": ["北京"], "target_city_code": "310000",
         "weekend_start": "2026-08-08", "weekend_end": "2026-08-10"},
        [{"city_code": "310000", "name": "上海"}],
    )
    cand = opts["candidates"][0]
    assert cand["recommended_mode"] == "compare"
    assert "rail" in cand["door_to_door"] and "air" in cand["door_to_door"]
    assert opts["prefill"]["rail"]["to"] == "上海"
    assert opts["presale"] and opts["presale"][0]["open_at"]  # 起售时间已算


def test_transport_never_fabricates_price_or_availability():
    """硬 KPI③（关联）：transport_options 不含票价/余票「字段键」（disclaimer 提及不算）。"""
    forbidden = {"price", "fare", "ticket_price", "seat_price", "availability",
                 "ticket_status", "price_amount", "stock"}

    def key_names(o) -> set[str]:
        out: set[str] = set()
        if isinstance(o, dict):
            for k, v in o.items():
                out.add(str(k).lower())
                out |= key_names(v)
        elif isinstance(o, list):
            for i in o:
                out |= key_names(i)
        return out

    for c in [{"origins": [], "target_city_code": "310000"},
              {"origins": ["北京"], "target_city_code": "310000",
               "weekend_start": "2026-08-08", "weekend_end": "2026-08-10"}]:
        opts = build_transport_options(c, [{"city_code": "310000", "name": "上海"}])
        hit = key_names(opts) & forbidden
        assert not hit, f"出现票价/余票字段键: {hit}"


def test_door_to_door_segments_have_sources():
    d2d = door_to_door((116.4, 39.9), (121.5, 31.2), mode="rail")
    assert d2d["total_min"] > 0
    assert set(d2d["evidence_by_seg"]) >= {"buffer_in", "run", "buffer_out"}
    # 所有证据非 confirmed（缓冲/运行一律 estimated）
    for ev in d2d["evidence_by_seg"].values():
        assert ev["verification_status"] == "estimated"


def test_estimate_door_to_door_monotone_with_distance():
    near = estimate_door_to_door((121.4, 31.2), (121.5, 31.3), "rail")
    far = estimate_door_to_door((116.4, 39.9), (121.5, 31.2), "rail")
    assert far["run_min"] > near["run_min"]


# —— DD-08 目的地 ——
def test_score_city_more_activities_higher():
    assert score_city(20, 120, True, 600) > score_city(1, 120, True, 600)


def test_destination_discovery_rich_card(session):
    state = {"constraints": {"target_city_code": "310000", "interests": ["展览"],
                             "weekend_start": "2026-07-25", "weekend_end": "2026-07-27"}}
    out = destination_discovery(state, session)
    card = out["candidate_cities"][0]
    assert card["city_code"] == "310000"
    assert card["evidence"]["verification_status"] != "confirmed_by_user"
    assert "driven_by_activities" in card and "recommended_transport" in card


def test_destination_discovery_missing_city_fallback(session):
    state = {"constraints": {"target_city_code": "999999", "weekend_start": "2026-07-25",
                             "weekend_end": "2026-07-27"}}
    out = destination_discovery(state, session)
    card = out["candidate_cities"][0]
    assert card["city_code"] == "999999"  # 兜底候选，不阻断
    assert card["evidence"]["verification_status"] == "estimated"


# —— 契约变更：静默默认值显式化（降级/兜底必须诚实标注）——
def test_parse_constraints_no_silent_target_default():
    """目的地留空 → 不再静默默认上海；兜底行为在 warnings 显式声明。"""
    c, warnings = parse_constraints({"origins": ["杭州"]})
    assert not c.get("target_city_code")  # 留空，由 discover 按 origin 同城推荐
    assert any("同城推荐" in w for w in warnings)


def test_build_rerank_query_empty_without_signals():
    """无兴趣/忌讳等个性化信号 → 空串（检索跳过 rerank），不再产出"周末不限 忌讳无"。"""
    assert build_rerank_query({}) == ""
    assert build_rerank_query({"interests": ["展览"]}) == "周末展览"
    assert build_rerank_query({"interests": ["展览"], "dietary": ["不吃辣"]}) == "周末展览 忌讳不吃辣"
    c, _ = parse_constraints({})
    assert c["query"] == ""  # 空串 → 检索层仅结构化过滤 + 时间窗


def test_destination_discovery_empty_target_same_city(session):
    """目的地留空 → discover 用解析出的出发地做同城推荐（primary=出发城）。"""
    state = {"constraints": {"origins": ["杭州"],
                             "weekend_start": "2026-07-25", "weekend_end": "2026-07-27"}}
    out = destination_discovery(state, session)
    assert out["candidate_cities"][0]["city_code"] == "330100"  # 杭州（origin）置首
    assert not out.get("warnings")  # origin 正常解析，无兜底 warning


def test_destination_discovery_origin_fallback_warns(session):
    """出发地解析不到城市 → 兜底上海并在 warnings 显式声明"按上海处理"（不静默）。"""
    state = {"constraints": {"origins": ["某某不存在城市"], "target_city_code": "310000",
                             "weekend_start": "2026-07-25", "weekend_end": "2026-07-27"}}
    out = destination_discovery(state, session)
    assert out["origin"]["city_code"] == "310000"
    assert any("按上海处理" in w for w in out["warnings"])


def test_presale_note_on_sale_when_open_at_passed():
    """起售时间已过 → 文案"已起售，请直接购买"（不再提醒等待起售）。"""
    tomorrow = date.today() + timedelta(days=1)
    opts = build_transport_options(
        {"origins": ["北京"], "target_city_code": "310000",
         "weekend_start": tomorrow.isoformat(),
         "weekend_end": (tomorrow + timedelta(days=2)).isoformat()},
        [{"city_code": "310000", "name": "上海"}],
    )
    assert opts["presale"][0]["note"] == "已起售，请直接购买"
