"""归一（DD-06 §5.5）：时间(Asia/Shanghai) + 场馆/坐标(高德 geocode via DD-04)。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import dateparser

from ..config import SHANGHAI_TZ
from ..providers import call as provider_call
from .dedup import make_fingerprint

_ZH_SETTINGS = {
    "TIMEZONE": "Asia/Shanghai",
    "RETURN_AS_TIMEZONE_AWARE": True,
    "PREFER_DAY_OF_MONTH": "first",
}
_CONTEMPORARY_YEAR_RE = re.compile(r"(?<!\d)(20[2-3]\d)(?!\d)")


class NormalizeError(Exception):
    """归一失败（如时间不可解析）→ 调用方入审核队列。"""


@dataclass
class NormActivity:
    title: str
    city_code: str
    venue: str | None
    category: str | None
    start_at: datetime | None
    end_at: datetime | None
    price_text: str | None
    booking_url: str | None
    location: tuple[float, float] | None  # (lng, lat) WGS84
    venue_id: int | None
    source_url: str
    source_type: object
    draft: object = field(repr=False, default=None)
    fingerprint: str | None = None

    def embed_text(self) -> str:
        return " ".join(
            filter(None, [self.title, self.venue or "", self.category or "", self.price_text or ""])
        )

    def conflicts_with(self, other: "NormActivity") -> bool:
        return (self.start_at != other.start_at) or (self.price_text != other.price_text)


def _parse_dt(value: str | None) -> datetime | None:
    """解析中文时间（Asia/Shanghai）。无年份时补当前年重试；支持相对日期（本周末/周六）。"""
    if not value:
        return None
    dt = dateparser.parse(value, languages=["zh"], settings=_ZH_SETTINGS)
    if dt is None:
        # 无年份（"7月23日"）→ 补当前年份重试
        from datetime import datetime as _dt
        dt = dateparser.parse(f"{_dt.now().year}年{value}", languages=["zh"], settings=_ZH_SETTINGS)
    return dt


def normalize_activity(d, src) -> NormActivity:
    """draft + 源 → NormActivity（时间归一、场馆 geocode、fingerprint）。失败抛 NormalizeError。"""
    title = (d.title or "").strip()
    if not title:
        raise NormalizeError("title missing")
    start_at = _parse_dt(d.start_text.value if d.start_text else None)
    if start_at is None:
        raise NormalizeError("start_at unparsable")
    end_at = _parse_dt(d.end_text.value if d.end_text else None)
    if end_at is not None and end_at < start_at:
        # N1：结束早于开始（倒序时间）→ 归一失败，入审核队列，不入库
        raise NormalizeError("end_at earlier than start_at (inverted range)")
    if end_at is not None and end_at.date() - start_at.date() > timedelta(days=370):
        # 搜索摘要常把不同年份的同月日拼成一个伪长展；此类结果不能自动入推荐库。
        raise NormalizeError("activity date span exceeds 370 days")
    title_years = {int(value) for value in _CONTEMPORARY_YEAR_RE.findall(title)}
    occurrence_years = {start_at.year}
    if end_at is not None:
        occurrence_years.add(end_at.year)
    if title_years and not (title_years & occurrence_years):
        # 标题声称“2025巡演”却只抽到 2026 日期，通常是搜索摘要把旧标题与新日期拼接。
        raise NormalizeError("title year conflicts with activity date")
    now = datetime.now(SHANGHAI_TZ)
    if (end_at or start_at) < now:
        # 活动整体已过期（DB 实证有 2007/2015 年旧活动以可信态入库）→ 入审核队列，不入库
        raise NormalizeError("activity already ended (past date)")

    location: tuple[float, float] | None = None
    if d.venue:
        res = provider_call("amap", "geocode", {"address": d.venue, "city": src.city_code})
        loc = (res.data or {}).get("location") if res.ok else None
        if isinstance(loc, list) and len(loc) == 2:
            location = (float(loc[0]), float(loc[1]))  # [lng, lat] → (lng, lat)

    n = NormActivity(
        title=title,
        city_code=src.city_code,
        venue=d.venue,
        category=d.category,
        start_at=start_at,
        end_at=end_at,
        price_text=(d.price.value if d.price else None),
        booking_url=(d.booking.value if d.booking else None),
        location=location,
        venue_id=None,
        source_url=d.source_url,
        source_type=src.source_type,
        draft=d,
    )
    n.fingerprint = make_fingerprint(n)
    return n
