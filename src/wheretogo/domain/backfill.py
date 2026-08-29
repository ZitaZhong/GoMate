"""DD-10 回填与抽取（BYO Booking）：三入口抽取 + 逐字段确认 + 时间线锚点。

铁律：**抽取只是初稿，确认才是事实**；未确认不入时间线。确认 → confirmed_by_user（DD-03 唯一
允许 user_provided → confirmed 的路径）。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from ..enums import SourceType
from ..providers import extract_fact
from ..schemas.evidence import Evidence

# —— 本地正则（无 LLM key 的兜底；与 extension/content.js 同源）——
_TRAIN_RE = re.compile(r"([GDCZTK]\d{1,4})")
_FLIGHT_RE = re.compile(r"([A-Z0-9]{2})\s?(\d{3,4})")
_DATE_RE = re.compile(r"(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}日|周[一二三四五六七日天])")
_TIME_RE = re.compile(r"(\d{1,2})[:：](\d{2})")
_LEG_RE = re.compile(r"([一-龥A-Za-z]{2,10})\s*(?:到|至|→|-|—)\s*([一-龥A-Za-z]{2,10})")


def local_parse_booking(text: str | None) -> dict:
    """本地正则解析订单文本（车次/航班/站/日期/时间）。无 LLM 也能抽，标 source=rule→estimated。"""
    t = text or ""
    draft: dict = {"kind": "manual", "extracted": {}}
    m_train = _TRAIN_RE.search(t)
    m_flight = _FLIGHT_RE.search(t)
    if m_train:
        draft["kind"] = "train"
        draft["extracted"]["train_no"] = m_train.group(1)
    elif m_flight:
        draft["kind"] = "flight"
        draft["extracted"]["flight_no"] = m_flight.group(1) + m_flight.group(2)
    elif ("酒店" in t) or ("住宿" in t) or ("入住" in t):
        draft["kind"] = "hotel"
    d = _DATE_RE.search(t)
    # 时间：兼容 "8:00" 与 "8点"/"8点30"
    times: list[str] = [f"{a}:{b}" for a, b in _TIME_RE.findall(t)]
    for a, b in re.findall(r"(\d{1,2})点(?:(\d{1,2}))?", t):
        hh = f"{a}:{int(b) if b else 0:02d}"
        if not any(tm.split(":")[0] == a for tm in times):
            times.append(hh)
    if d:
        draft["extracted"]["date"] = d.group(1)
    if len(times) >= 1:
        draft["extracted"]["dep_time"] = times[0]
    if len(times) >= 2:
        draft["extracted"]["arr_time"] = times[1]
    # 站段匹配：先剔除车次/航班号与时间/日期/席别/币值/周几，避免 "8点上海虹桥" 把"点"带进站名
    t_leg = t
    for m in (m_train, m_flight):
        if m:
            t_leg = t_leg.replace(m.group(0), " ")
    t_leg = re.sub(
        r"\d+[:：]?\d*|点|早上|上午|下午|晚上|早晨|周[一二三四五六日天]|次|[一二三四五六七]等座|硬座|软座|元|RMB|¥|\s",
        " ", t_leg)
    leg = _LEG_RE.search(t_leg)
    if leg and draft["kind"] != "manual":
        if draft["kind"] == "flight":
            draft["extracted"]["dep_airport"] = leg.group(1)
            draft["extracted"]["arr_airport"] = leg.group(2)
        else:
            draft["extracted"]["from_station"] = leg.group(1)
            draft["extracted"]["to_station"] = leg.group(2)
    if draft["kind"] == "hotel":
        nm = re.search(r"([一-龥A-Za-z·]{2,20}(?:大酒店|酒店|宾馆|民宿|公寓|客栈|饭店|店))", t)
        if nm:
            draft["extracted"]["name"] = nm.group(1)
    return draft


_SCHEMA_BY_KIND: dict[str, dict[str, str]] = {
    "train": {"train_no": "车次", "from_station": "出发站", "to_station": "到达站",
              "dep_time": "出发时间", "arr_time": "到达时间", "date": "乘车日期"},
    "flight": {"flight_no": "航班号", "dep_airport": "出发机场", "arr_airport": "到达机场",
               "dep_time": "起飞时间", "arr_time": "到达时间", "date": "日期"},
    "hotel": {"name": "酒店名", "check_in": "入住日期", "check_out": "退房日期", "area": "区域"},
}
_REQUIRED: dict[str, list[str]] = {
    "train": ["from_station", "to_station", "date"],
    "flight": ["dep_airport", "arr_airport", "date"],
    "hotel": ["name", "check_in"],
}


def _user_ev() -> dict:
    return Evidence(source_type=SourceType.user_provided,
                    verification_status="confirmed_by_user", confidence=1.0,
                    fetched_at=datetime.now(timezone.utc)).to_jsonb()


def run_extract(kind: str, input_kind: str, raw: str | None, byo_key: str | None = None) -> dict:
    """三入口抽取初稿：本地正则兜底（无 key 也抽）+ LLM 增强（有 key 时补充/纠错）。

    manual 无 raw → 空初稿（待手工表单）；有 raw → 本地正则必抽，LLM 可选增强。
    """
    schema = _SCHEMA_BY_KIND.get(kind, {})
    if not raw:
        return {"kind": kind, "input_kind": input_kind, "extracted": {}, "ready_for_resume": False}
    # ① 本地正则兜底（PRD 原则三 BYO Booking：无 LLM 也能结构化）
    local = local_parse_booking(raw)
    extracted: dict = dict(local.get("extracted") or {})
    kind_resolved = kind if kind and kind != "manual" else local.get("kind", kind or "manual")
    # ② LLM 增强（有 key 时补充字段；无 key 跳过，不阻断）
    task = "booking_ocr" if input_kind == "image" else "activity_extract"
    facts = extract_fact(task, raw, schema, source_type=SourceType.llm, byo_key=byo_key)
    if facts:
        extracted.update({k: f.value for k, f in facts.items() if f.value is not None})
    return {"kind": kind_resolved, "input_kind": input_kind, "extracted": extracted,
            "ready_for_resume": False, "source": "llm" if facts else "local_regex"}


def confirm_booking(draft: dict, confirmed_fields: dict | None = None) -> dict:
    """逐字段确认：用户确认值覆盖初稿；关键字段齐全才 ready_for_resume；evidence→confirmed_by_user。"""
    extracted = dict(draft.get("extracted") or {})
    merged = {**extracted, **(confirmed_fields or {})}
    ready = all(merged.get(f) for f in _REQUIRED.get(draft.get("kind"), []))
    return {"kind": draft.get("kind"), "input_kind": draft.get("input_kind"),
            "extracted": merged, "confirmed": ready, "evidence": _user_ev()}


def _try_dt(date_str: str | None, time_str: str | None):
    """date(ISO/中文) + time("8:00") → Asia/Shanghai datetime。

    非 ISO（"7月24日"/"周六"）用 dateparser 补当前年解析，使交通锚点成为定时槽位（修 P2/P1-6a）。
    """
    if not date_str:
        return None
    from ..config import SHANGHAI_TZ
    hh = mm = 0
    if time_str:
        tm = re.match(r"(\d{1,2})[:：](\d{0,2})", str(time_str))
        if tm:
            hh, mm = int(tm.group(1)), int(tm.group(2) or 0)
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", date_str)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), hh, mm, tzinfo=SHANGHAI_TZ)
    try:
        import dateparser
        settings = {"TIMEZONE": "Asia/Shanghai", "RETURN_AS_TIMEZONE_AWARE": True,
                    "PREFER_DATES_FROM": "future"}
        dt = dateparser.parse(date_str, languages=["zh"], settings=settings)
        if dt is None:  # 无年份（"7月24日"）→ 补当前年重试
            dt = dateparser.parse(f"{datetime.now().year}年{date_str}", languages=["zh"], settings=settings)
        if dt is not None:
            return dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except Exception:
        return None
    return None


def to_timeline_anchors(bookings: list[dict] | None) -> list[dict]:
    """已确认 bookings → 时间线锚点（transport/lodging，带时刻）；evidence 恒 confirmed_by_user。"""
    anchors: list[dict] = []
    for b in (bookings or []):
        kind = b.get("kind")
        ex = b.get("extracted") or {}
        ev = b.get("evidence") or _user_ev()
        if kind in ("train", "flight"):
            frm = ex.get("from_station") or ex.get("dep_airport") or "出发"
            to = ex.get("to_station") or ex.get("arr_airport") or "到达"
            times = " ".join(t for t in (ex.get("dep_time"), ex.get("arr_time")) if t)
            start_at = _try_dt(ex.get("date"), ex.get("dep_time"))
            end_at = _try_dt(ex.get("date"), ex.get("arr_time"))
            if start_at and end_at and end_at <= start_at:
                end_at = end_at + timedelta(days=1)  # 跨零点（夜车/红眼）→ 到达次日
            anchors.append({
                "kind": "transport", "title": f"{frm} → {to}" + (f" {times}" if times else ""),
                "start_at": start_at, "end_at": end_at,
                "ref_table": "bookings", "evidence": ev,
            })
        elif kind == "hotel":
            anchors.append({"kind": "lodging", "title": ex.get("name") or "酒店",
                            "start_at": _try_dt(ex.get("check_in"), None),
                            "end_at": _try_dt(ex.get("check_out"), None),
                            "ref_table": "bookings", "evidence": ev})
    return anchors


def build_resume_payload(bookings: list[dict] | None) -> list[dict]:
    """供 DD-02 Command(resume=...) 的 payload（逐字一致契约）。"""
    return list(bookings or [])
