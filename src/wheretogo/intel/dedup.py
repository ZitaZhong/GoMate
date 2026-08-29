"""去重 & 实体对齐（DD-06 §5.6）：fingerprint 一级 + pgvector 二级(cos<=0.12)。"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..retrieval.service import RetrievalService

DEDUP_COS_THRESHOLD = 0.12  # cosine distance <= 阈值视为同一活动

# PRD 05 来源可信度 → trust_level（DD-06 §5.1）
TRUST: dict[str, int] = {
    "official_venue": 1, "culture_bureau": 2, "open_dataset": 3, "search": 4,
    "user_provided": 5, "editorial": 6, "community": 7,
}

_svc: RetrievalService | None = None

_TRADITIONAL_TITLE_MAP = str.maketrans({
    "陸": "陆", "這": "这", "樣": "样", "許": "许", "張": "张", "劉": "刘",
    "陳": "陈", "趙": "赵", "與": "与", "樂": "乐", "會": "会", "臺": "台",
    "灣": "湾", "體": "体", "館": "馆", "場": "场", "藝": "艺", "術": "术",
    "國": "国", "聲": "声", "經": "经", "區": "区", "門": "门", "遊": "游",
    "戲": "戏", "劇": "剧", "紅": "红", "無": "无", "來": "来", "現": "现",
})


def _get_svc() -> RetrievalService:
    global _svc
    if _svc is None:
        _svc = RetrievalService()
    return _svc


def _norm_title(t: str | None) -> str:
    return re.sub(r"\s+", "", re.sub(r"[【】\[\]（）()·—\-—:：]", "", t or "")).lower()


def normalize_event_title(title: str | None) -> str:
    """生成跨来源活动实体比较用标题。

    搜索结果常把城市、年份、站次、品类标签拼进标题，同一演出会因此得到不同
    fingerprint。这里保留艺人/主题等辨识信息，仅去掉展示层噪声。
    """
    value = unicodedata.normalize("NFKC", title or "").lower().strip()
    value = value.translate(_TRADITIONAL_TITLE_MAP)
    value = re.sub(r"\s*[-—–]\s*[\u4e00-\u9fffa-z0-9. ]{2,12}\s*$", "", value)
    value = re.sub(r"^[\u4e00-\u9fff]{2,4}\s*[·•:：]\s*", "", value)
    value = re.sub(r"(?:19|20)\d{2}", "", value)
    value = re.sub(
        r"(?:北京|上海|广州|深圳|杭州|南京|苏州|成都|重庆|天津|武汉|西安|"
        r"长沙|郑州|合肥|宁波|厦门|青岛|济南|福州|昆明|南昌|沈阳|大连|"
        r"哈尔滨|长春|石家庄|太原|南宁|海口|贵阳|乌鲁木齐)站$",
        "",
        value,
    )
    value = re.sub(
        r"(?:世界|全国)?(?:巡回|巡迴)?演唱[会會]|"
        r"(?:世界|全国)?(?:巡回|巡迴)?巡演|"
        r"(?:巡回|巡迴)|音乐[会會]|音樂[会會]|live(?:现场|現場)?",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)


def same_event_title(left: str | None, right: str | None) -> bool:
    """判断两个跨来源标题是否指向同一活动实体。"""
    a = normalize_event_title(left)
    b = normalize_event_title(right)
    if min(len(a), len(b)) < 4:
        return False
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    if len(shorter) >= 5 and shorter in longer:
        return True
    matcher = SequenceMatcher(None, a, b)
    if matcher.ratio() >= 0.78:
        return True
    # 营销前后缀差异很大时，稳定的活动专名仍会形成较长公共片段，
    # 如“官方特惠…宋城千古情”和“暑期特惠…宋城千古情”。
    common = matcher.find_longest_match(0, len(a), 0, len(b))
    return common.size >= 5


def make_fingerprint(n) -> str:
    """一级去重键：hash(标准化标题 + 城市 + 场馆 + 开始日期)。"""
    day = n.start_at.astimezone().date().isoformat() if n.start_at else ""
    basis = f"{_norm_title(n.title)}|{n.city_code}|{_norm_title(n.venue or '')}|{day}"
    return hashlib.sha1(basis.encode()).hexdigest()


@dataclass
class DupRef:
    id: int
    trust_level: int
    matched: str  # fingerprint | semantic
    dist: float | None = None


def find_duplicate(n, session: Session) -> DupRef | None:
    """命中重复返回 DupRef（含主记录可信度），用于合并/覆盖决策；否则 None。"""
    row = session.execute(
        text("SELECT id, evidence->>'source_type' AS st FROM activities WHERE fingerprint = :fp"),
        {"fp": n.fingerprint},
    ).first()
    if row:
        return DupRef(id=row.id, trust_level=TRUST.get(row.st, 9), matched="fingerprint")

    # fingerprint 对标题/场馆的细微写法差异很敏感。先在同城且日期重叠的有限集合中
    # 做确定性实体对齐，避免同一演出从不同来源反复生成新 activity id。
    start_day = n.start_at.date() if n.start_at else None
    end_day = (n.end_at or n.start_at).date() if (n.end_at or n.start_at) else start_day
    if start_day:
        rows = session.execute(
            text(
                "SELECT id, title, evidence->>'source_type' AS st FROM activities "
                "WHERE city_code = :city AND start_at::date <= :end_day "
                "AND COALESCE(end_at, start_at)::date >= :start_day "
                "AND expires_at > now() ORDER BY start_at DESC LIMIT 100"
            ),
            {
                "city": n.city_code,
                "start_day": start_day,
                "end_day": end_day or start_day,
            },
        ).all()
        for candidate in rows:
            if same_event_title(n.title, candidate.title):
                return DupRef(
                    id=candidate.id,
                    trust_level=TRUST.get(candidate.st, 9),
                    matched="entity_title",
                )

    emb = _get_svc().embed([n.embed_text()])[0]
    day = n.start_at.date() if n.start_at else None
    r = session.execute(
        text(
            "SELECT id, evidence->>'source_type' AS st, "
            "embedding <=> CAST(:vec AS vector) AS dist FROM activities "
            "WHERE city_code = :city AND start_at::date = :day AND expires_at > now() "
            "ORDER BY embedding <=> CAST(:vec AS vector) LIMIT 1"
        ),
        {"vec": str(emb), "city": n.city_code, "day": day},
    ).first()
    if r and r.dist is not None and float(r.dist) <= DEDUP_COS_THRESHOLD:
        return DupRef(id=r.id, trust_level=TRUST.get(r.st, 9), matched="semantic", dist=float(r.dist))
    return None
