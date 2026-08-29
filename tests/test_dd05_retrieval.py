"""DD-05 检索服务验收测试（对应 DD-05 §10 DoD）。

用确定性 fallback provider，覆盖：召回硬过滤（不编造）、BM25 专有名词召回、
个性化重排、以及 §9 三级降级路径与餐饮“动线+忌讳”重排。
"""
from __future__ import annotations

import logging

from wheretogo.enums import VerificationStatus
from wheretogo.retrieval import RetrievalService, rrf
from wheretogo.retrieval.providers import (
    DashScopeEmbeddingProvider,
    HashingEmbeddingProvider,
    LexicalRerankProvider,
)


def _svc() -> RetrievalService:
    return RetrievalService(HashingEmbeddingProvider(), LexicalRerankProvider())


class _DownEmbed(HashingEmbeddingProvider):
    available = False


class _DownRerank(LexicalRerankProvider):
    available = False


def test_rrf_orders_by_reciprocal_rank():
    fused = rrf([[1, 2, 3], [3, 4, 1]])
    assert set(fused) == {1, 2, 3, 4}
    assert fused[0] in (1, 3)  # 同时出现在两路 → 排前


def test_recall_returns_only_trusted(session, weekend, make_activity):
    """DoD #4 不编造：召回硬过滤后只剩官方/公开确认态。"""
    make_activity("上海周末 官方展览", verification_status=VerificationStatus.official_source_confirmed)
    make_activity("上海周末 公开观察展", verification_status=VerificationStatus.public_source_observed)
    make_activity("上海周末 估算展", verification_status=VerificationStatus.estimated)
    make_activity("上海周末 未知展", verification_status=VerificationStatus.unknown)

    res = _svc().retrieve_activities(session, "310000", weekend, {"query": "上海周末"}, top_k=20)
    assert res
    assert {c.verification_status for c in res} <= {"official_source_confirmed", "public_source_observed"}


def test_sparse_recall_finds_proper_noun(session, weekend, make_activity):
    """DoD #1 召回全：专有 token 经 BM25 精确召回。"""
    target = make_activity("梵高展 星空沉浸")
    make_activity("普通 周末市集")
    res = _svc().retrieve_activities(session, "310000", weekend, {"query": "星空沉浸"}, top_k=20)
    assert target.id in {c.id for c in res}


def test_personalization_changes_ranking(session, weekend, make_activity):
    """DoD #3 个性化：同城不同偏好 → 明显不同排序。"""
    a = make_activity("上海周末 莫奈油画展览", category="展览")
    b = make_activity("上海周末 周杰伦演唱会演出", category="演出")

    top_exhibit = _svc().retrieve_activities(
        session, "310000", weekend, {"query": "上海周末", "interests": ["展览", "油画", "莫奈"]}, top_k=2
    )
    top_show = _svc().retrieve_activities(
        session, "310000", weekend, {"query": "上海周末", "interests": ["演出", "演唱会", "周杰伦"]}, top_k=2
    )
    assert top_exhibit[0].id == a.id
    assert top_show[0].id == b.id


def test_evidence_passthrough(session, weekend, make_activity):
    """结果透传 evidence（DD-03），不新造事实。"""
    make_activity("上海周末 官方展览")
    res = _svc().retrieve_activities(session, "310000", weekend, {"query": "上海周末"}, top_k=5)
    assert res[0].evidence.get("source_type") == "official_venue"


def test_degrade_pure_bm25_when_embedding_down(session, weekend, make_activity):
    """§9：embedding 挂 → 退化纯 BM25 仍返回。"""
    make_activity("上海周末 展览A")
    svc = RetrievalService(_DownEmbed(), LexicalRerankProvider())
    res = svc.retrieve_activities(session, "310000", weekend, {"query": "上海周末"}, top_k=5)
    assert res


def test_degrade_rrf_order_when_rerank_down(session, weekend, make_activity):
    """§9：rerank 挂 → 退化 RRF 序（无重排分）。"""
    make_activity("上海周末 展览A")
    svc = RetrievalService(HashingEmbeddingProvider(), _DownRerank())
    res = svc.retrieve_activities(session, "310000", weekend, {"query": "上海周末"}, top_k=5)
    assert res and all(c.rerank_score is None for c in res)


def test_degrade_structured_when_both_down(session, weekend, make_activity):
    """§9：二者都挂 → 结构化过滤 + start_at 兜底仍返回。"""
    make_activity("上海周末 展览A")
    svc = RetrievalService(_DownEmbed(), _DownRerank())
    res = svc.retrieve_activities(session, "310000", weekend, {"query": "上海周末"}, top_k=5)
    assert res


def test_retrieve_dining_distance_and_dietary():
    """DD-05 §7：按动线契合(距离)重排 + 排除忌讳 + 永远保留稳妥备选。"""
    near = (121.4737, 31.2304)
    pois = [
        {"name": "本帮菜 清淡", "location": (121.4740, 31.2306), "cuisine": "本帮菜", "evidence": {}},
        {"name": "辣味火锅", "location": (121.4750, 31.2310), "cuisine": "川菜", "evidence": {}},
        {"name": "远方西餐", "location": (121.90, 31.90), "cuisine": "西餐", "evidence": {}},
        {"name": "稳妥快餐", "location": (121.60, 31.50), "cuisine": "快餐", "is_fallback": True, "evidence": {}},
    ]
    res = _svc().retrieve_dining(near, "lunch", {"dietary": ["辣"]}, pois=pois, top_k=3)
    names = [c.name for c in res]
    assert "辣味火锅" not in names  # 忌讳被排除
    assert res[0].name == "本帮菜 清淡"  # 近 + 无忌讳排最前
    assert any(c.is_fallback for c in res)  # 保留稳妥备选


# —— embedding 诚实性：版本如实标注 + API 失败熔断 ——
def test_embedding_version_honest_for_hashing():
    """哈希兜底的 version 如实标注 hashing-ngram-v1，不冒名 API 模型版本。"""
    assert HashingEmbeddingProvider().version == "hashing-ngram-v1"


def test_dashscope_embed_circuit_breaker(monkeypatch, caplog):
    """embed API 连续失败 N 次 → 熔断：本进程直接走哈希（不再发 HTTP）、记 warning、version 如实。"""
    posts = {"n": 0}

    class _BoomClient:
        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a) -> bool:
            return False

        def post(self, *a, **k):
            posts["n"] += 1
            raise RuntimeError("boom")

    monkeypatch.setattr("httpx.Client", _BoomClient)
    DashScopeEmbeddingProvider._circuit_open = False
    DashScopeEmbeddingProvider._circuit_failures = 0
    try:
        p = DashScopeEmbeddingProvider()
        with caplog.at_level(logging.WARNING):
            for _ in range(DashScopeEmbeddingProvider._CIRCUIT_THRESHOLD):
                out = p.embed(["x"])
                assert len(out) == 1  # 失败仍返回哈希兜底，管线不阻断
        assert DashScopeEmbeddingProvider._circuit_open is True
        assert any("熔断" in r.message for r in caplog.records)  # 熔断记 warning
        assert p.version == "hashing-ngram-v1"  # 熔断后如实标注，不冒名 API 版本
        before = posts["n"]
        p.embed(["x"])  # 熔断中：不再发 HTTP（不再每次白跑超时）
        assert posts["n"] == before
    finally:
        DashScopeEmbeddingProvider._circuit_open = False
        DashScopeEmbeddingProvider._circuit_failures = 0
