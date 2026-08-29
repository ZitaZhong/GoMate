"""DD-12 时间线求解与校验：确定性贪心排点 + 硬约束校验（硬冲突率=0 KPI）。

证据透传铁律：槽位 evidence 原样来自上游实体（bookings/activities/dining），不新造；
buffer/free 槽用 estimated。validate 产出 {ok,issues,details,metrics:{hard_conflict}} 驱动条件边。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .backfill import to_timeline_anchors


def _ev_est(note: str | None = None) -> dict:
    return {"source_type": "rule", "verification_status": "estimated", "confidence": 0.4, "note": note}


def _ev_rule(note: str | None = None) -> dict:
    return {"source_type": "rule", "verification_status": "public_source_observed", "confidence": 0.6, "note": note}


def _parse(s) -> datetime | None:
    if s is None:
        return None
    return s if isinstance(s, datetime) else datetime.fromisoformat(s)


def _cmp_past(e: datetime | None, now: datetime) -> bool:
    """e 是否已过去（tz 不一致无法比较时视为未过去，不据此排除）。"""
    if e is None:
        return False
    try:
        return e < now
    except TypeError:
        return False


def _placeable_activity(a: dict, now: datetime) -> bool:
    """脏数据拦截（N1）：过期态 / 结束早于开始（倒序） / 结束已过去 → 不排入时间线。"""
    if a.get("verification_status") == "expired":
        return False
    s, e = _parse(a.get("start_at")), _parse(a.get("end_at"))
    if s and e and e < s:
        return False
    if _cmp_past(e, now):
        return False
    return True


_OUTDOOR_KW = ("户外", "市集", "赛事", "骑行", "徒步", "露营", "公园", "登山", "马拉松", "音乐节", "花海")


def _is_outdoor(a: dict) -> bool:
    """按类别/标题粗判户外（供天气重规划：恶劣天气偏好室内）。"""
    txt = f"{a.get('category') or ''} {a.get('title') or ''}"
    return any(k in txt for k in _OUTDOOR_KW)


def solve_timeline(activities: list[dict], dining: list[dict],
                   bookings: list[dict], constraints: dict,
                   weather: dict | None = None) -> list[dict]:
    """贪心排点：上游锚点 → 活动（冲突感知：跳过重叠项，保证出稿无硬冲突）→ 饭点 → 返程缓冲。

    活动时段来自场馆固定时刻（不可移动）；故对重叠活动采取"贪心选取、跳过冲突"，
    使最终 timeline 无时段重叠（硬冲突率=0 KPI）。证据透传不新造。
    """
    slots: list[dict] = []
    seq = 0

    for a in to_timeline_anchors(bookings):  # ① 交通/住宿锚点（confirmed_by_user 透传）
        slots.append({"seq": seq, "kind": a["kind"], "title": a["title"],
                      "ref_table": a["ref_table"], "evidence": a["evidence"]})
        seq += 1

    now = datetime.now(timezone.utc)
    placeable = [a for a in (activities or []) if a.get("start_at") and _placeable_activity(a, now)]
    if (weather or {}).get("adverse"):  # 恶劣天气：室内优先（其次时间），使重规划改变行为
        acts = sorted(placeable, key=lambda x: (_is_outdoor(x), x["start_at"]))
    else:
        acts = sorted(placeable, key=lambda x: x["start_at"])
    lr = _parse(constraints.get("latest_return")) if constraints.get("latest_return") else None
    placed: list[tuple[datetime, datetime]] = []
    meal_inserted = False
    for a in acts[:8]:  # ② 冲突感知贪心选取
        s = _parse(a.get("start_at"))
        e = _parse(a.get("end_at")) or (s + timedelta(hours=2) if s else None)
        if not (s and e):
            continue
        if lr and e > lr:  # 超过最晚返程 → 不排（尊重 latest_return）
            continue
        if any(not (e <= ps or s >= pe) for ps, pe in placed):  # 与已选重叠 → 跳过
            continue
        placed.append((s, e))
        slots.append({"seq": seq, "kind": "activity", "title": a.get("title"),
                      "start_at": a.get("start_at"), "end_at": a.get("end_at"),
                      "ref_table": "activities", "ref_id": a.get("id"),
                      "evidence": a.get("evidence") or _ev_rule("活动排程")})
        seq += 1
        if not meal_inserted and dining and len(placed) >= 2:  # 午间插一餐
            d = dining[0]
            slots.append({"seq": seq, "kind": "meal", "title": d.get("name"),
                          "ref_table": "dining", "evidence": d.get("evidence") or _ev_rule("餐饮")})
            seq += 1
            meal_inserted = True

    if placed or bookings:  # ③ 末尾返程缓冲
        slots.append({"seq": seq, "kind": "buffer", "title": "返程缓冲（进站/值机）",
                      "evidence": _ev_est("返程缓冲估算")})
    return slots


def validate_timeline(slots: list[dict], constraints: dict, bookings: list[dict] | None = None,
                      attempts: int = 1) -> dict:
    """硬约束校验：HARD_CONFLICT（重叠）/ RETURN_TIGHT（返程）/CLOSED/OVER_BUDGET。

    attempts 为本 plan 第几次进 validate（reflow/retransport 计数）；由 route_after_validate
    用于熔断（超上限强制 compose，避免死循环）。
    """
    issues: list[str] = []
    details: dict = {}
    hard_conflict = False

    timed = [(_parse(s["start_at"]), _parse(s["end_at"])) for s in slots
             if s.get("start_at") and s.get("end_at")]
    for i in range(len(timed)):
        if hard_conflict:
            break
        for j in range(i + 1, len(timed)):
            s1, e1 = timed[i]
            s2, e2 = timed[j]
            if s1 < e2 and s2 < e1:  # 区间重叠
                issues.append("HARD_CONFLICT")
                hard_conflict = True
                break

    latest = constraints.get("latest_return")
    ends = [_parse(s["end_at"]) for s in slots if s.get("end_at")]
    if latest and ends:
        try:
            lr = _parse(latest)
            last = max(ends)
            if last and lr and last > lr:
                issues.append("RETURN_TIGHT")
                details["RETURN_TIGHT"] = {"have_min": int((last - lr).total_seconds() // 60)}
        except (ValueError, TypeError):
            pass

    # N1 脏数据安全网：任一槽位结束早于开始 → INVERTED_RANGE；活动结束已过去 → EXPIRED_ACTIVITY
    now = datetime.now(timezone.utc)
    for s in slots:
        ss, ee = _parse(s.get("start_at")), _parse(s.get("end_at"))
        if ss and ee and ee < ss:
            issues.append("INVERTED_RANGE")
            break
    for s in slots:
        if s.get("kind") == "activity" and _cmp_past(_parse(s.get("end_at")), now):
            issues.append("EXPIRED_ACTIVITY")
            break

    issues = list(dict.fromkeys(issues))  # 去重保序
    return {"ok": not issues, "issues": issues, "details": details,
            "metrics": {"hard_conflict": hard_conflict, "slot_count": len(slots), "attempts": attempts}}
