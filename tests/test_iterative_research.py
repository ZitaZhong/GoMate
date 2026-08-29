"""多轮迭代活动调研 E2E 测试（对标 Researchify reflect-loop）。

验证要点：
1. NLU 消歧：对结果不满→deep_research（非 refine_field）
2. 研究回环：用户不满意→重搜→新结果（不重复）
3. 状态累积：research_history/shown_activity_ids 跨轮追加
4. 熔断机制：超 max_loops 后不再回环
5. BFF 闭环：__research_feedback 触发 auto_stream
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from wheretogo.bff.app import app
from wheretogo.orchestration.nodes import (
    _REFLECT_MAX_LOOPS,
    _generate_follow_up_queries,
    activity_reflection,
)

client = TestClient(app)


# ═══════════════════════════════════════════════════════════════════
# 4.4 NLU 消歧测试
# ═══════════════════════════════════════════════════════════════════


def test_nlu_disambiguate_deep_research_vs_refine():
    """‘不喜欢这些还有没有别的’ → deep_research（不是 refine_field）。"""
    from wheretogo.copilot.handle_turn import classify_intent

    # 对结果不满 → deep_research（规则降级匹配）
    assert classify_intent("这几个我不喜欢，还有没有其他的演唱会", use_llm=False) == "deep_research"
    assert classify_intent("还有别的选择吗", use_llm=False) == "deep_research"
    assert classify_intent("能不能再搜搜", use_llm=False) == "deep_research"
    assert classify_intent("不太满意，再找找", use_llm=False) == "deep_research"

    # 主动改字段 → refine_field（关键词“改成”匹配 refine_field）
    assert classify_intent("目的地改成苏州", use_llm=False) == "refine_field"
    assert classify_intent("我不想看演唱会了 我想逛展", use_llm=False) == "refine_field"


# ═══════════════════════════════════════════════════════════════════
# 4.1 深度测试：handle_turn deep_research handler
# ═══════════════════════════════════════════════════════════════════


def test_chat_deep_research_without_prior_graph_starts_initial_stream():
    """没有上一轮 checkpoint 时不能伪续流，应从现有约束启动首轮完整规划。"""
    pid = client.post("/plans", json={"constraints": {
        "origins": ["上海"], "interests": ["演唱会"], "target_city_code": "310000",
        "weekend_start": "2026-07-31T00:00:00+08:00",
        "weekend_end": "2026-08-02T23:59:59+08:00",
    }}).json()["plan_id"]

    r = client.post(f"/plans/{pid}/chat",
                    json={"message": "这几个我不喜欢，还有没有其他的演唱会"})
    d = r.json()
    # intent 应为 deep_research（不论 LLM 是否可用，规则降级也匹配 "有没有"）
    assert d["intent"] == "deep_research"
    assert d["action"] == "invoke"
    assert d.get("auto_stream") is False
    assert d.get("restart_stream") is True
    assert "首轮方案" in d["reply"]


# ═══════════════════════════════════════════════════════════════════
# 4.2 activity_reflection 节点单元测试
# ═══════════════════════════════════════════════════════════════════


def test_reflection_no_feedback_passes_through():
    """无反馈 + 有活动 → 充分，直接通过。"""
    state = {
        "activities": [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}, {"id": 3, "title": "C"}],
        "research_feedback": None,
        "research_loop_count": 0,
        "shown_activity_ids": [],
        "constraints": {"interests": ["展览"]},
        "candidate_cities": [{"name": "上海", "city_code": "310000"}],
    }
    result = activity_reflection(state)
    # 不应产生 follow_up_queries
    assert "follow_up_queries" not in result or not result.get("follow_up_queries")
    # shown_activity_ids 应记录当前活动
    assert 1 in result["shown_activity_ids"]


def test_reflection_with_feedback_generates_follow_up():
    """有反馈 → 生成 follow_up_queries + 记录反思历史。"""
    state = {
        "activities": [{"id": 1, "title": "演唱会A"}, {"id": 2, "title": "演唱会B"}],
        "research_feedback": "这几个我不喜欢，还有没有其他的演唱会",
        "research_loop_count": 0,
        "shown_activity_ids": [],
        "constraints": {"interests": ["演唱会"]},
        "candidate_cities": [{"name": "杭州", "city_code": "330100"}],
    }
    result = activity_reflection(state)
    # 应生成 follow_up_queries
    assert result.get("follow_up_queries") and len(result["follow_up_queries"]) >= 1
    # loop_count 递增
    assert result["research_loop_count"] == 1
    # 反馈被清空
    assert result.get("research_feedback") is None
    # research_history 有记录
    assert result.get("research_history") and len(result["research_history"]) == 1
    assert result["research_history"][0]["feedback"] == "这几个我不喜欢，还有没有其他的演唱会"


# ═══════════════════════════════════════════════════════════════════
# 4.3 route_after_reflect 条件路由
# ═══════════════════════════════════════════════════════════════════


def test_route_after_reflect_loops_on_feedback():
    """有反馈 + 未超限 → 回环到 research。"""
    from wheretogo.orchestration.graph import route_after_reflect

    state = {"research_feedback": "不喜欢", "research_loop_count": 1, "activities": [{"id": 1}]}
    assert route_after_reflect(state) == "research"


def test_route_after_reflect_continues_when_sufficient():
    """无反馈 + 有活动 → 继续到 transport。"""
    from wheretogo.orchestration.graph import route_after_reflect

    state = {"research_feedback": None, "research_loop_count": 1, "activities": [{"id": 1}]}
    assert route_after_reflect(state) == "transport"


def test_route_after_reflect_fuses_at_max():
    """有反馈但已超上限 → 强制进 transport（熔断）。"""
    from wheretogo.orchestration.graph import route_after_reflect

    state = {"research_feedback": "还想看更多", "research_loop_count": _REFLECT_MAX_LOOPS,
             "activities": [{"id": 1}]}
    assert route_after_reflect(state) == "transport"


def test_route_honors_explicit_continue_at_limit_until_reflect_finalizes():
    """显式 continue 表示最后一批查询尚未执行，不能提前停在 searching。"""
    from wheretogo.orchestration.graph import route_after_reflect

    state = {
        "research_feedback": None,
        "research_loop_count": _REFLECT_MAX_LOOPS,
        "research_should_continue": True,
        "activities": [{"id": 1}],
    }
    assert route_after_reflect(state) == "research"


# ═══════════════════════════════════════════════════════════════════
# 4.5 _generate_follow_up_queries 降级测试
# ═══════════════════════════════════════════════════════════════════


def test_generate_follow_up_queries_fallback():
    """无 LLM 时规则降级也能生成查询。"""
    queries = _generate_follow_up_queries(
        interests=["演唱会"],
        feedback="不喜欢这些",
        exclude_titles=["洛天依巡演", "陆虎巡演"],
        city_name="杭州",
    )
    assert len(queries) >= 2
    # 应包含城市名
    assert any("杭州" in q for q in queries)


# ═══════════════════════════════════════════════════════════════════
# 4.1 多轮迭代 E2E：活动不重复 + 状态累积
# ═══════════════════════════════════════════════════════════════════


def test_multi_turn_research_activities_no_repeat(session, make_activity, monkeypatch):
    """核心 E2E：activity_research + activity_reflection 多轮不重复。

    模拟 2 轮：
    - 轮1：初始检索得到部分活动
    - 轮2：设置 feedback + shown_ids → 重搜应排除轮1活动

    注意：activity_research 内部经 get_session() 自建连接，看不到 `session`
    夹具事务里的未提交行。因此本用例的活动改为**显式提交**（finally 清理），
    否则轮1 只能撞运气依赖库中已提交的同类活动（历史上因此偶发失败）。
    """
    from wheretogo.db import get_session
    from wheretogo.orchestration.nodes import activity_research
    from wheretogo.retrieval import Weekend

    wk = Weekend(datetime(2026, 7, 25, tzinfo=timezone.utc),
                 datetime(2026, 7, 27, tzinfo=timezone.utc))

    # 准备 6 个活动（带可搜索的标题）：提交落库，使节点自建 session 可见
    made_ids: list[int] = []
    with get_session() as s:
        from wheretogo.retrieval.providers import HashingEmbeddingProvider
        from wheretogo.enums import VerificationStatus
        from wheretogo.models import Activity
        emb = HashingEmbeddingProvider()
        for i in range(6):
            t = f"演出活动{chr(65 + i)}"
            a = Activity(
                title=t, city_code="310000", venue="", category="演出", price_text=None,
                evidence={"source_type": "official_venue",
                          "verification_status": "official_source_confirmed",
                          "confidence": 0.9},
                verification_status=VerificationStatus.official_source_confirmed,
                embedding=emb.embed([f"{t}  演出 "])[0],
                start_at=datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc), end_at=None,
                expires_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
            )
            s.add(a)
            s.flush()
            made_ids.append(a.id)
        s.commit()

    try:
        # 轮1：有 query 可触发检索
        state1 = {
            "constraints": {"interests": ["演出"], "query": "演出活动",
                            "target_city_code": "310000",
                            "weekend_start": wk.start.isoformat(), "weekend_end": wk.end.isoformat()},
            "candidate_cities": [{"city_code": "310000", "name": "上海"}],
            "shown_activity_ids": [],
            "follow_up_queries": [],
        }
        r1 = activity_research(state1)
        ids1 = [a["id"] for a in (r1.get("activities") or [])]
        assert len(ids1) >= 1, "轮1应返回活动"
        # 轮1 命中的必须是本次提交的活动（不受库中其它已提交数据影响）
        assert any(i in made_ids for i in ids1), "轮1应命中本用例提交的活动"

        # 轮2：设置 shown_activity_ids = ids1 → 应排除轮1活动
        state2 = {
            **state1,
            "shown_activity_ids": ids1,
            "follow_up_queries": ["演出活动 最新 特色"],  # gap-driven
        }
        r2 = activity_research(state2)
        ids2 = [a["id"] for a in (r2.get("activities") or [])]

        # 关键断言：轮2不含轮1的活动
        overlap = set(ids1) & set(ids2)
        assert not overlap, f"轮2不应重复轮1的活动，重复: {overlap}"

        # 如果有轮2结果，验证它们确实是新的
        if ids2:
            assert all(i not in ids1 for i in ids2), "轮2活动应全部是新的"
    finally:
        with get_session() as s:
            s.query(Activity).filter(Activity.id.in_(made_ids)).delete(
                synchronize_session=False)
            s.commit()


def test_reflection_loop_count_increments():
    """研究轮次累积递增。"""
    state = {
        "activities": [{"id": 1, "title": "X"}],
        "research_feedback": "不满意",
        "research_loop_count": 2,
        "shown_activity_ids": [1],
        "constraints": {"interests": ["演出"]},
        "candidate_cities": [{"name": "上海", "city_code": "310000"}],
    }
    r = activity_reflection(state)
    assert r["research_loop_count"] == 3
