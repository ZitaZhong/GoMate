"""召回层：混合检索的 SQL（DD-05 §5，一条 SQL 打三重条件）。

硬过滤铁律（DD-05 §5.4）：稠密与稀疏召回都强制 `verification_status IN 可信态`，
不给编造留位——核心活动只来自官方/公开确认源。
"""
from __future__ import annotations

from datetime import datetime

from geoalchemy2 import WKTElement
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..enums import TRUSTED_STATUSES
from ..models import Activity

_TRUSTED = list(TRUSTED_STATUSES)


def _center_geog(center: tuple[float, float] | None) -> WKTElement | None:
    if center is None:
        return None
    lng, lat = center
    return WKTElement(f"POINT({lng} {lat})", srid=4326)


def _overlaps(wk_start: datetime, wk_end: datetime):
    """SQL 时间区间重叠：活动开始不晚于窗口结束，且结束不早于窗口开始。"""
    return (
        Activity.start_at <= wk_end,
        func.coalesce(Activity.end_at, Activity.start_at) >= wk_start,
    )


def dense_recall(
    session: Session,
    city_code: str,
    wk_start: datetime,
    wk_end: datetime,
    q_vec: list[float],
    center: tuple[float, float] | None = None,
    radius_m: int = 30000,
    limit: int = 100,
) -> list[int]:
    """稠密召回（带硬过滤 + 可选地理），按余弦距离升序取 top-N id。"""
    stmt = select(Activity.id).where(
        Activity.city_code == city_code,
        *_overlaps(wk_start, wk_end),
        Activity.verification_status.in_(_TRUSTED),
        Activity.expires_at > func.now(),
        Activity.embedding.is_not(None),
    )
    geog = _center_geog(center)
    if geog is not None:
        stmt = stmt.where(func.ST_DWithin(Activity.location, geog, radius_m))
    stmt = stmt.order_by(Activity.embedding.cosine_distance(q_vec)).limit(limit)
    return list(session.scalars(stmt))


def sparse_recall(
    session: Session,
    city_code: str,
    wk_start: datetime,
    wk_end: datetime,
    q_text: str,
    limit: int = 100,
) -> list[int]:
    """稀疏召回（BM25/全文，同硬过滤 + verification_status 铁律）。"""
    tsq = func.plainto_tsquery("simple", q_text)
    stmt = (
        select(Activity.id)
        .where(
            Activity.city_code == city_code,
            *_overlaps(wk_start, wk_end),
            Activity.verification_status.in_(_TRUSTED),
            Activity.expires_at > func.now(),
            Activity.search_tsv.op("@@")(tsq),
        )
        .order_by(func.ts_rank(Activity.search_tsv, tsq).desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def structured_recall(
    session: Session,
    city_code: str,
    wk_start: datetime,
    wk_end: datetime,
    limit: int = 100,
) -> list[int]:
    """降级兜底（§9：embedding+rerank 都不可用）：结构化过滤 + start_at 排序。"""
    stmt = (
        select(Activity.id)
        .where(
            Activity.city_code == city_code,
            *_overlaps(wk_start, wk_end),
            Activity.verification_status.in_(_TRUSTED),
            Activity.expires_at > func.now(),
        )
        .order_by(Activity.start_at.asc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def load_rows(session: Session, ids: list[int]) -> list[Activity]:
    """批量取回 activities 行并保持给定顺序（含 evidence，供透传）。"""
    if not ids:
        return []
    rows = session.scalars(select(Activity).where(Activity.id.in_(ids))).all()
    by_id = {r.id: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def load_coords(session: Session, ids: list[int]) -> dict[int, tuple[float, float]]:
    """批量取活动坐标 {id: (lng, lat)}（供 DD-11 接驳真实估算；无坐标的行不返回）。"""
    if not ids:
        return {}
    rows = session.execute(text(
        "SELECT id, ST_X(location::geometry) lng, ST_Y(location::geometry) lat "
        "FROM activities WHERE id = ANY(:ids) AND location IS NOT NULL"
    ), {"ids": list(ids)}).all()
    return {r[0]: (float(r[1]), float(r[2])) for r in rows if r[1] is not None}
