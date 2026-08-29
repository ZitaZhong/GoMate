"""检索服务（DD-05 §3、§6、§7、§9）。

- retrieve_activities：混合召回 top-100 → 交叉重排 → top_k；结果透传 activities.evidence。
- retrieve_dining：POI 候选按“动线契合(距离)+偏好+忌讳”重排，永远保留一个稳妥备选。
- 降级阶梯完全对齐 §9：embedding 挂→纯 BM25；rerank 挂→RRF 序；都挂→结构化+start_at。
v0.1 采用同步实现（psycopg 同步 + 无 GPU），BFF 可放线程池；接口语义与 DD-05 一致。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from ..config import get_settings
from .providers import (
    EmbeddingProvider,
    RerankProvider,
    get_embedding_provider,
    get_rerank_provider,
)
from .recall import dense_recall, load_coords, load_rows, sparse_recall, structured_recall
from .rerank import rerank_query, rerank_query_dining, rerank_text
from .rrf import rrf


@dataclass
class Weekend:
    start: datetime
    end: datetime


@dataclass
class ActivityCandidate:
    """activities 行 + rerank_score，evidence 原样透传（DD-03）。"""

    id: int
    title: str
    city_code: str | None
    venue: str | None
    category: str | None
    price_text: str | None
    booking_url: str | None
    start_at: datetime | None
    end_at: datetime | None
    verification_status: str
    availability_status: str
    evidence: dict
    rerank_score: float | None = None
    location: tuple[float, float] | None = None  # (lng, lat)；供 DD-11 接驳真实估算

    @classmethod
    def from_row(cls, row, score: float | None) -> "ActivityCandidate":
        vs = row.verification_status
        av = row.availability_status
        return cls(
            id=row.id,
            title=row.title,
            city_code=row.city_code,
            venue=row.venue,
            category=row.category,
            price_text=row.price_text,
            booking_url=row.booking_url,
            start_at=row.start_at,
            end_at=row.end_at,
            verification_status=vs.value if hasattr(vs, "value") else vs,
            availability_status=av.value if hasattr(av, "value") else av,
            evidence=row.evidence,
            rerank_score=score,
        )


@dataclass
class DiningCandidate:
    name: str
    location: tuple[float, float] | None
    cuisine: str | None
    distance_m: int | None
    is_fallback: bool
    evidence: dict
    rerank_score: float | None = None
    meta: dict = field(default_factory=dict)


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """两经纬度点间的大圆距离（米）。"""
    (lng1, lat1), (lng2, lat2) = a, b
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


class RetrievalService:
    def __init__(
        self,
        embedder: EmbeddingProvider | None = None,
        reranker: RerankProvider | None = None,
    ) -> None:
        self.embedder = embedder if embedder is not None else get_embedding_provider()
        self.reranker = reranker if reranker is not None else get_rerank_provider()

    # —— 供 DD-06 入库 / 内部使用 ——
    def embed(self, texts: list[str]) -> list[list[float]]:
        return self.embedder.embed(texts)

    def embedding_version(self) -> str:
        """实际产出向量的版本标识（哈希兜底时如实 'hashing-ngram-v1'，不冒名 API 模型）。"""
        return getattr(self.embedder, "version", None) or get_settings().embedding_version

    def rerank(self, query: str, docs: list[str]) -> list[float]:
        return self.reranker.score(query, docs)

    # —— 活动检索（DD-08 research 节点调用）——
    def retrieve_activities(
        self,
        session: Session,
        city_code: str,
        weekend: Weekend,
        constraints: dict,
        top_k: int = 20,
        exclude_ids: set[int] | None = None,
    ) -> list[ActivityCandidate]:
        q_text = (constraints.get("query") or "").strip()
        center = constraints.get("center")
        radius = int(constraints.get("radius_m", 30000))
        emb_ok = self.embedder.available
        rr_ok = self.reranker.available

        # §9 情形④：embedding 与 rerank 都不可用 → 结构化过滤 + start_at。
        # 无检索 query（DD-07：无兴趣/忌讳等个性化信号时不再拼废话串）→
        # 同走结构化召回（城市 + 时间窗），并跳过重排（rerank 对空 query 无意义）。
        if (not emb_ok and not rr_ok) or not q_text:
            rows = load_rows(
                session, structured_recall(session, city_code, weekend.start, weekend.end)
            )
            cands = [ActivityCandidate.from_row(r, None) for r in rows[:top_k]]
            self._attach_coords(session, cands)
            return cands

        # —— 召回 ——
        sparse_ids = (
            sparse_recall(session, city_code, weekend.start, weekend.end, q_text) if q_text else []
        )
        dense_ids: list[int] = []
        if emb_ok and q_text:
            q_vec = self.embed([q_text])[0]
            dense_ids = dense_recall(
                session, city_code, weekend.start, weekend.end, q_vec, center, radius
            )

        # —— 融合（embedding 不可用 → 仅 BM25）——
        if dense_ids and sparse_ids:
            fused = rrf([dense_ids, sparse_ids])[:100]
        else:
            fused = (dense_ids or sparse_ids)[:100]

        if not fused:
            return []  # §9 召回为空 → 上层触发 DD-06 补搜 / 官方源清单

        rows = load_rows(session, fused)

        # —— 重排（不可用 → 保持 RRF 序）——
        if rr_ok:
            scores = self.rerank(rerank_query(constraints), [rerank_text(r) for r in rows])
            order = sorted(range(len(rows)), key=lambda i: scores[i], reverse=True)
            ranked = [(rows[i], scores[i]) for i in order]
        else:
            ranked = [(r, None) for r in rows]

        cands = [ActivityCandidate.from_row(r, s) for r, s in ranked[:top_k]]
        # 研究迭代：排除已展示过的活动（对标 Researchify fetched_urls 去重）
        if exclude_ids:
            cands = [c for c in cands if c.id not in exclude_ids]
        self._attach_coords(session, cands)
        return cands

    def _attach_coords(self, session: Session, cands: list[ActivityCandidate]) -> None:
        """批量回填活动坐标（供 DD-11 dining/mobility 真实接驳估算）。"""
        coords = load_coords(session, [c.id for c in cands])
        for c in cands:
            c.location = coords.get(c.id)

    # —— 餐饮检索（DD-11 dining 节点复用；修“找餐馆不妥帖”）——
    def retrieve_dining(
        self,
        near: tuple[float, float],
        meal_slot: str,
        constraints: dict,
        pois: list[dict] | None = None,
        top_k: int = 3,
    ) -> list[DiningCandidate]:
        """POI 召回（由 DD-04 poi_search 传入 `pois`）后，按动线契合+偏好+忌讳重排。

        与“丢一个评分榜单”的区别：距离是重排强特征，且忌讳项被排除、永远留一个稳妥备选。
        """
        pois = pois or []
        dietary = [d for d in (constraints.get("dietary") or [])]
        rq = rerank_query_dining(constraints, meal_slot)

        scored: list[DiningCandidate] = []
        for poi in pois:
            loc = poi.get("location")
            loc_t = tuple(loc) if isinstance(loc, (list, tuple)) else None
            dist = int(_haversine_m(near, loc_t)) if loc_t else None
            name = poi.get("name", "")
            cuisine = poi.get("cuisine")
            text = f"{name} {cuisine or ''}"
            # 忌讳命中直接排除（不进候选，除非是 fallback）
            excluded = any(tag and tag in text for tag in dietary)
            # 词法相关性（偏好契合）
            rel = self.reranker.score(rq, [text])[0] if self.reranker.available else 0.0
            # 动线契合：距离越近分越高（1km 半衰）
            prox = 1.0 / (1.0 + (dist or 0) / 1000.0) if dist is not None else 0.5
            score = 0.6 * prox + 0.4 * rel
            cand = DiningCandidate(
                name=name,
                location=loc_t,
                cuisine=cuisine,
                distance_m=dist,
                is_fallback=bool(poi.get("is_fallback")),
                evidence=poi.get("evidence", {}),
                rerank_score=round(score, 4),
                meta={"excluded_by_dietary": excluded},
            )
            if not excluded or cand.is_fallback:
                scored.append(cand)

        scored.sort(key=lambda c: c.rerank_score or 0.0, reverse=True)
        result = scored[:top_k]

        # 永远保留一个稳妥备选（DD-11）：若结果里没有 fallback，尝试补一个
        if result and not any(c.is_fallback for c in result):
            fb = next((c for c in scored if c.is_fallback), None)
            if fb is not None:
                result = result[: max(0, top_k - 1)] + [fb]
        return result
