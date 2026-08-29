"""锚点路线设计（DD-15 v1.1 增补）：用户点名场馆/活动（"既要A也要B"）时，
把锚点解析到库内活动并排出一条可执行的周末日路线。

设计纪律（与 DD-03 对齐）：
- 锚点解析只读库内可信态活动；匹配不到的名字不丢弃，以 unknown/待确认保留并显式标注；
- 场次/营业时间一律不编造——库内有确切时间的用真实值（透传原 evidence），
  没有的一律 estimated 占位并提示"以官方/票面为准"；
- 接驳时间为规则粗估（estimated），不做精确路线承诺。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..enums import TRUSTED_STATUSES
from ..models import Activity

_EV_EST = {"source_type": "rule", "verification_status": "estimated", "confidence": 0.4}
_EV_UNKNOWN = {"source_type": "rule", "verification_status": "unknown", "confidence": 0.2}

_EVENING_KINDS = ("演唱会", "演出", "音乐会", "livehouse", "live", "话剧", "音乐剧")
_MEAL_LUNCH = ("12:30", "13:30")
_CONCERT_DEFAULT = ("19:00", "21:30")
_MUSEUM_SLOTS = [("10:00", "12:30"), ("14:00", "16:30")]


def _is_evening(activity: dict) -> bool:
    text = f"{activity.get('category') or ''} {activity.get('title') or ''}".lower()
    return any(k in text for k in _EVENING_KINDS)


def _norm(s: str) -> str:
    """匹配前归一化：去引号/书名号/括号/空白——避免「“万兽之王”」与「万兽之王」
    因标点错位导致正确行与泛匹配打平（实测被同名演唱会顶替）。"""
    return "".join(ch for ch in (s or "") if ch not in "「」“”\"'‘’（）()【】<>《》·• \t")


def _longest_common(a: str, b: str) -> int:
    """两串最长公共子串长度（归一化后；简单 DP，够用于锚点匹配）。"""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ca in a:
        cur = [0]
        for j, cb in enumerate(b, 1):
            cur.append(prev[j - 1] + 1 if ca == cb else 0)
            best = max(best, cur[-1])
        prev = cur
    return best


def resolve_anchors(
    names: list[str],
    session: Session,
    city_code: str | None,
    wk_start: datetime,
    wk_end: datetime,
    limit: int = 300,
) -> tuple[list[dict], list[str]]:
    """把用户点名的锚点解析到库内可信未过期活动。

    匹配规则：venue 命中（名字是 venue 的子串或互为子串且 ≥4 字）或
    与 title 最长公共子串 ≥4 字；每个名字取最优一条。返回 (resolved, pending)。
    """
    q = session.query(Activity).filter(
        Activity.verification_status.in_(list(TRUSTED_STATUSES)),
        Activity.expires_at > datetime.now(wk_start.tzinfo),
        Activity.start_at <= wk_end,
    )
    if city_code:
        q = q.filter(Activity.city_code == city_code)
    rows = q.order_by(Activity.start_at.asc()).limit(limit).all()

    resolved: list[dict] = []
    used: set[int] = set()
    pending: list[str] = []
    for name in names:
        best_row: Activity | None = None
        best_score = 0
        nname = _norm(name)
        for row in rows:
            if row.id in used:
                continue
            score = _longest_common(name, row.title or "")
            venue = _norm(row.venue or "")
            if len(nname) >= 4 and (nname in venue or (len(venue) >= 4 and venue in nname)):
                score = max(score, len(nname) + 2)  # 场馆命中加权
            if score > best_score:
                best_row, best_score = row, score
        if best_row is not None and best_score >= 4:
            used.add(best_row.id)
            resolved.append({
                "id": best_row.id,
                "matched_name": name,
                "title": best_row.title,
                "venue": best_row.venue,
                "category": best_row.category,
                "start_at": best_row.start_at.isoformat() if best_row.start_at else None,
                "end_at": best_row.end_at.isoformat() if best_row.end_at else None,
                "evidence": best_row.evidence,
            })
        else:
            pending.append(name)
    return resolved, pending


def design_day_route(
    anchors: list[dict],
    pending: list[str],
    weekend_start: datetime,
    weekend_end: datetime,
) -> dict:
    """把锚点排成周末日路线（museum 白天 / 演出晚间，就近排序，含接驳与用餐占位）。

    返回 route_plan：{days, warnings, anchors_resolved, anchors_pending}。
    所有规则估算的字段统一 estimated 证据；库内活动透传原 evidence。
    """
    days = _weekend_days(weekend_start, weekend_end)
    if not days:
        days = [weekend_start.date()]
    warnings: list[str] = [
        "场次/营业时间以官方页面或票面为准，路线时间为规划估算（estimated）",
    ]
    concerts = [a for a in anchors if _is_evening(a)]
    daytimes = [a for a in anchors if not _is_evening(a)]
    # 待确认锚点（未在库中核实到）按用户指定保留，排在第一天白天，unknown 证据
    pending_slots = [{"title": name, "pending": True} for name in pending]
    if pending:
        warnings.append(
            "「" + "、".join(pending) + "」未在库中核实到具体活动，已按你的指定保留为待确认锚点，"
            "建议提供官方链接以便核实"
        )

    slots_by_day: list[list[dict]] = [[] for _ in days]
    # 白天锚点按天均分（每天最多 2 个白天活动，避免赶场）
    di = 0
    for i, a in enumerate(daytimes):
        di = min(i // 2, len(days) - 1)
        slots_by_day[di].append(a)
    for p in pending_slots:
        slots_by_day[0].append(p)
    # 晚间演出：有确切时间的按真实日期归位，否则放最后一天晚间
    for c in concerts:
        placed = False
        start_iso = c.get("start_at")
        if start_iso:
            try:
                d = datetime.fromisoformat(start_iso).date()
                for i, day in enumerate(days):
                    if d == day:
                        slots_by_day[i].append(c)
                        placed = True
                        break
            except (ValueError, TypeError):
                pass
        if not placed:
            slots_by_day[-1].append(c)

    out_days = []
    for date, items in zip(days, slots_by_day):
        if not items and (daytimes or concerts or pending_slots):
            continue
        slots: list[dict] = []
        day_items = [a for a in items if not a.get("pending")]
        pending_items = [a for a in items if a.get("pending")]
        museum_slots = [a for a in day_items if not _is_evening(a)]
        concert_slots = [a for a in day_items if _is_evening(a)]

        prev_venue: str | None = None
        cursor = 0
        for a in museum_slots:
            s, e = _MUSEUM_SLOTS[min(cursor, len(_MUSEUM_SLOTS) - 1)]
            cursor += 1
            if prev_venue and a.get("venue") != prev_venue:
                slots.append(_leg(prev_venue, a.get("venue")))
            slots.append(_activity_slot(a, date, s, e, estimated_time=True))
            prev_venue = a.get("venue")
        for p in pending_items:
            slots.append({
                "kind": "activity",
                "title": p["title"],
                "start": f"{date.isoformat()}T{_MUSEUM_SLOTS[0][0]}:00",
                "end": f"{date.isoformat()}T{_MUSEUM_SLOTS[0][1]}:00",
                "note": "待确认：未在库中核实到该活动，请提供官方链接",
                "evidence": dict(_EV_UNKNOWN),
            })
        if museum_slots or pending_items:
            slots.append(_meal(date, "午餐", *_MEAL_LUNCH))
        for c in concert_slots:
            s_iso, e_iso = c.get("start_at"), c.get("end_at")
            concrete = False
            if s_iso:
                try:
                    sdt = datetime.fromisoformat(s_iso)
                    concrete = sdt.date() == date
                except (ValueError, TypeError):
                    concrete = False
            if prev_venue and c.get("venue") != prev_venue:
                slots.append(_leg(prev_venue, c.get("venue")))
            if concrete:
                start_t = datetime.fromisoformat(s_iso).strftime("%H:%M")
                if e_iso:
                    try:
                        end_t = datetime.fromisoformat(e_iso).strftime("%H:%M")
                    except (ValueError, TypeError):
                        end_t = _CONCERT_DEFAULT[1]
                else:
                    end_t = _CONCERT_DEFAULT[1]
                slots.append(_activity_slot(c, date, start_t, end_t, estimated_time=False))
            else:
                slots.append(_meal(date, "晚餐", "17:30", "18:45"))
                slots.append(_activity_slot(c, date, *_CONCERT_DEFAULT, estimated_time=True,
                                            note="常驻/巡演场次，具体场次以官方或票面为准"))
            prev_venue = c.get("venue")

        out_days.append({"date": date.isoformat(), "slots": slots})

    return {
        "days": out_days,
        "warnings": warnings,
        "anchors_resolved": len(anchors),
        "anchors_pending": list(pending),
        "evidence_note": "路线编排为规则求解；时间/接驳为估算（estimated），不做精确承诺",
    }


def _weekend_days(start: datetime, end: datetime) -> list:
    days = []
    d = start.date()
    while d <= end.date():
        days.append(d)
        d += timedelta(days=1)
    return days[:3]  # 周末行程最多排 3 天


def _activity_slot(a: dict, date, start_t: str, end_t: str, *,
                   estimated_time: bool, note: str | None = None) -> dict:
    return {
        "kind": "activity",
        "title": a.get("title"),
        "venue": a.get("venue"),
        "start": f"{date.isoformat()}T{start_t}:00",
        "end": f"{date.isoformat()}T{end_t}:00",
        "note": note,
        "evidence": dict(_EV_EST) if estimated_time else (a.get("evidence") or dict(_EV_EST)),
    }


def _meal(date, label: str, start_t: str, end_t: str) -> dict:
    return {
        "kind": "meal",
        "title": f"{label}（建议就近解决，待餐饮推荐）",
        "start": f"{date.isoformat()}T{start_t}:00",
        "end": f"{date.isoformat()}T{end_t}:00",
        "evidence": dict(_EV_EST),
    }


def _leg(from_venue: str | None, to_venue: str | None) -> dict:
    return {
        "kind": "leg",
        "title": f"{from_venue or '上一点'} → {to_venue or '下一点'}",
        "note": "市内接驳约 30–45 分钟（粗估，建议在地图 App 确认）",
        "evidence": dict(_EV_EST),
    }
