"""DD-13 Trip Bundle 组装：最终闸 + 提醒 + ICS。

- run_final_gate：出稿前必跑 Guard（KPI① 未确认误展=0）+ 交通禁编闸三（KPI③）。
- build_reminders：九类提醒规格（pre_trip_72h/activity_start/return_trip/presale…）。
- build_ics / build_ics_fallback：纯本地 RFC5545 日历（零落盘，恒可生成）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..config import SHANGHAI_TZ

_NINE_TYPES = (
    "presale", "activity_booking", "flight_recheck", "pre_trip_72h", "weather_24h",
    "doc_check", "hotel_cancel_deadline", "activity_start", "return_trip",
)
_CHANNELS = ("web_push", "email", "ics")
# 规则计算的提醒时点一律标 estimated（诚实：以官方平台当前页面为准）
_EV_RULE = {"source_type": "rule", "verification_status": "estimated"}


def run_final_gate(bundle: dict) -> dict:
    """DD-13 §5.3 最终闸：Guard + 交通禁编闸三；任一违规抛出（出稿前必过）。返回计数（期望 0）。"""
    # 惰性导入避免 domain → orchestration 的加载期循环
    from ..orchestration.guard import assert_guard, assert_no_fabricated_transport, run_guard

    guard_violations = run_guard(bundle)
    assert_no_fabricated_transport(bundle)  # KPI③：交通字段不得来自 LLM（违例抛 ProvenanceViolation）
    assert_guard(bundle)                    # KPI①：未确认误展为已确认 = 0（违例抛 ProvenanceViolation）
    return {"guard_violations": len(guard_violations), "fabricated_transport_count": 0}


def build_reminders(bundle: dict) -> list[dict]:
    """从 bundle 推导提醒规格（九类 × 默认三通道），payload 遵循 DD-13 §3.4（title/body/…）。

    fire_at 按 DD-13 §5.5 规则计算（规则时点一律标 estimated）：
    presale=transport.presale[*].open_at（DD-09 起售时刻）；flight_recheck=行前 5 天（仅可能选飞机时）；
    doc_check=行前 48h；pre_trip_72h/weather_24h=行前 72/24h；activity_start/return_trip=时间线首末。
    算不出的保留 fire_at=None（persist 跳过，不静默编造时点）：
    activity_booking 需活动 booking_open_at（当前活动模型无此字段）；
    hotel_cancel_deadline 需回填订单 cancel_deadline（当前回填不抽取该字段）。
    """
    rows: list[dict] = []
    for p in _logical_reminders(bundle):
        for ch in _CHANNELS:
            rows.append({"type": p["type"], "channel": ch,
                         "fire_at": p.get("fire_at"), "payload": p})
    return rows


def build_presale_reminders(transport: dict, plan_id=None) -> list[dict]:
    """透传 DD-09 transport_options.presale → presale 提醒 payload（DD-13 §5.5，起售时点即 fire_at）。"""
    rail_prefill = (transport.get("prefill") or {}).get("rail") or {}
    out: list[dict] = []
    for p in transport.get("presale") or []:
        open_at = p.get("open_at")
        route = (p.get("route") or "").strip()
        out.append(_reminder_payload(
            "presale", f"{route} 高铁起售提醒".strip(),
            f"车次将于 {_fmt_dt(open_at)} 起售；已备好预填与候补建议",
            open_at, plan_id, action_url="https://www.12306.cn/", prefill=rail_prefill,
            disclaimer=p.get("disclaimer") or "起售时间以 12306 当前页面为准",
            evidence=p.get("evidence")))
    return out


def reminders_preview(reminders: list[dict]) -> list[dict]:
    """DD-13 §3.3：提醒可读摘要（§3.4 子集，供前端展示“将提醒你…”）。

    只收已排时点（fire_at 非空、真正会被调度）的提醒；三通道副本合并为一条。
    兼容直接传 payload 列表（探索版 presale 预览）。
    """
    seen: set = set()
    out: list[dict] = []
    for r in reminders or []:
        p = r.get("payload") or r
        fire = r.get("fire_at") or p.get("fire_at")
        if not fire:
            continue
        key = (p.get("type"), p.get("title"), fire)
        if key in seen:
            continue
        seen.add(key)
        out.append({"type": p.get("type"), "title": p.get("title"),
                    "body": p.get("body"), "fire_at": fire})
    return out


def _logical_reminders(bundle: dict) -> list[dict]:
    """九类逻辑提醒（未扇出通道），顺序与 _NINE_TYPES 对齐。"""
    plan_id = bundle.get("plan_id")
    timeline = bundle.get("timeline") or []
    activities = [s for s in timeline if s.get("kind") == "activity" and s.get("start_at")]
    trip_start = _trip_start(bundle, activities)
    transport = bundle.get("transport") or {}

    out = build_presale_reminders(transport, plan_id)
    if not out:  # 无起售数据 → 占位（fire_at=None 不调度），保持九类规格完整
        out.append(_reminder_payload(
            "presale", "高铁起售提醒", "暂无起售时间数据；请到 12306 查询起售时间",
            None, plan_id, action_url="https://www.12306.cn/",
            disclaimer="起售时间以 12306 当前页面为准"))

    # activity_booking：DD-13 规则=活动带 booking_open_at 时逐条提醒；
    # 当前活动模型无 booking_open_at 字段 → 占位（fire_at=None 不调度）
    booking_open = [
        _reminder_payload(
            "activity_booking", f"{a.get('title') or '活动'} 预约开放",
            "预约/购票已开放，请及时预订；以官方页面为准",
            a["booking_open_at"], plan_id, action_url=a.get("booking_url"),
            evidence=a.get("evidence"))
        for a in (bundle.get("activities") or []) if a.get("booking_open_at")
    ]
    out.extend(booking_open or [_reminder_payload(
        "activity_booking", "活动预约提醒",
        "部分活动需提前预约/购票；请到官方页面查看预约开放时间", None, plan_id)])

    # flight_recheck：DD-13 规则=行前 5 天、仅当选飞机（rail/local 策略不排，不臆测飞行需求）
    cands = transport.get("candidates") or []
    mode = cands[0].get("recommended_mode") if cands else None
    flight_fire = (trip_start - timedelta(days=5)) if (
        trip_start and mode not in ("rail", "local")) else None
    out.append(_reminder_payload(
        "flight_recheck", "机票复查", "行前复查：确认航班时刻/航站楼/退改签规则",
        flight_fire, plan_id, disclaimer="以航司/OTA 当前页面为准"))

    out.append(_reminder_payload(
        "pre_trip_72h", "行前 72h 确认", "确认交通/住宿/活动安排是否有变",
        (trip_start - timedelta(hours=72)) if trip_start else None, plan_id))
    out.append(_reminder_payload(
        "weather_24h", "行前 24h 天气检查", "查看目的地天气，恶劣天气时启用室内备选",
        (trip_start - timedelta(hours=24)) if trip_start else None, plan_id))
    out.append(_reminder_payload(
        "doc_check", "证件检查", "出行前检查：身份证/购票证件随身携带",
        (trip_start - timedelta(hours=48)) if trip_start else None, plan_id))

    # hotel_cancel_deadline：DD-13 规则=回填酒店订单带 cancel_deadline 时逐条提醒；
    # 当前回填不抽取 cancel_deadline → 占位（fire_at=None 不调度）
    cancel = [
        _reminder_payload(
            "hotel_cancel_deadline", "酒店免费取消截止",
            "酒店免费取消即将截止，如行程有变请及时处理",
            cd, plan_id, evidence=b.get("evidence"))
        for b in (bundle.get("bookings") or [])
        if b.get("kind") == "hotel"
        for cd in [(b.get("extracted") or {}).get("cancel_deadline")] if cd
    ]
    out.extend(cancel or [_reminder_payload(
        "hotel_cancel_deadline", "酒店免费取消截止提醒",
        "预订酒店后请留意免费取消截止时间（以订单页为准）", None, plan_id)])

    first = activities[0] if activities else None
    out.append(_reminder_payload(
        "activity_start",
        f"活动开场：{first.get('title')}" if first and first.get("title") else "活动开场提醒",
        "活动即将开始，请提前到场", _first_start(activities), plan_id, alarm_before_min=60))
    out.append(_reminder_payload(
        "return_trip", "返程提醒", "预留进站/值机缓冲，别误了返程车次/航班",
        _last_end(timeline), plan_id, alarm_before_min=120))
    return out


def _reminder_payload(rtype: str, title: str, body: str, fire_at, plan_id, *,
                      action_url=None, prefill=None, disclaimer=None,
                      evidence=None, alarm_before_min: int = 0) -> dict:
    """DD-13 §3.4 统一 payload；规则计算的时点 evidence 缺省 rule/estimated。"""
    fire = fire_at.isoformat() if isinstance(fire_at, datetime) else fire_at
    return {
        "plan_id": plan_id, "type": rtype, "title": title, "body": body,
        "action_url": action_url, "prefill": prefill, "fire_at": fire,
        "ics": {"summary": title, "dtstart": fire, "duration_min": 15,
                "alarm_before_min": alarm_before_min},
        "disclaimer": disclaimer,
        "evidence": evidence or _EV_RULE,
    }


def _trip_start(bundle: dict, activities) -> datetime | None:
    """行程起点：优先约束时间窗 depart（earliest_depart/weekend_start），否则首个活动时间。"""
    depart = (bundle.get("time_windows") or {}).get("depart")
    if depart:
        try:
            return _parse(depart)
        except (ValueError, TypeError):
            pass
    return _first_start(activities)


def _fmt_dt(v) -> str:
    try:
        dt = _parse(v)
        return f"{dt.month}月{dt.day}日 {dt:%H:%M}"
    except (ValueError, TypeError):
        return str(v or "时间待定")


def build_ics(bundle: dict) -> str:
    """RFC5545 iCalendar（CRLF、零落盘）；活动槽位 → VEVENT，时间按 Asia/Shanghai 本地输出。"""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//WhereToGo//Weekend Plan//ZH",
             "BEGIN:VTIMEZONE", "TZID:Asia/Shanghai", "BEGIN:STANDARD",
             "DTSTART:19700101T000000", "TZOFFSETFROM:+0800", "TZOFFSETTO:+0800",
             "END:STANDARD", "END:VTIMEZONE"]
    for s in (bundle.get("timeline") or []):
        if s.get("kind") != "activity" or not s.get("start_at"):
            continue
        start = _ics_dt_local(s["start_at"])
        end = _ics_dt_local(s.get("end_at")) or start
        lines += ["BEGIN:VEVENT", f"UID:{s.get('ref_id')}@wheretogo",
                  f"SUMMARY:{s.get('title', '活动')}",
                  f"DTSTART;TZID=Asia/Shanghai:{start}", f"DTEND;TZID=Asia/Shanghai:{end}",
                  "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def build_ics_fallback(bundle: dict) -> str:
    """兜底日历：含一个行前提醒 VEVENT（带 DTSTART，合规 RFC5545）。"""
    plan_id = bundle.get("plan_id")
    dt = _ics_dt_local(None)  # 即时提醒时刻
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//WhereToGo//Fallback//ZH",
             "BEGIN:VTIMEZONE", "TZID:Asia/Shanghai", "BEGIN:STANDARD",
             "DTSTART:19700101T000000", "TZOFFSETFROM:+0800", "TZOFFSETTO:+0800",
             "END:STANDARD", "END:VTIMEZONE",
             "BEGIN:VEVENT", f"UID:fallback-{plan_id}@wheretogo", "SUMMARY:周末出行提醒",
             f"DTSTART;TZID=Asia/Shanghai:{dt}", "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def _first_start(activities) -> datetime | None:
    for a in activities:
        try:
            return _parse(a["start_at"])
        except (ValueError, TypeError, KeyError):
            continue
    return None


def _last_end(timeline) -> datetime | None:
    ends = []
    for s in timeline:
        if s.get("end_at"):
            try:
                ends.append(_parse(s["end_at"]))
            except (ValueError, TypeError):
                pass
    return max(ends) if ends else None


def _parse(s) -> datetime:
    return s if isinstance(s, datetime) else datetime.fromisoformat(s)


def _ics_dt_local(s) -> str:
    """ISO 时间 → Asia/Shanghai 本地 `YYYYMMDDTHHMMSS`（配 VTIMEZONE/TZID）。"""
    dt = _parse(s) if s else datetime.now(SHANGHAI_TZ)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI_TZ)
    return dt.astimezone(SHANGHAI_TZ).strftime("%Y%m%dT%H%M%S")
