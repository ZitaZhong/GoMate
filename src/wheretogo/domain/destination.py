"""DD-08 目的地发现（discover 节点领域实现）：当周活动驱动的城市推荐 + 加权评分。

v0.1 单目标城市（target_city_code）→ 1 张富信息城市卡（字段带 evidence）。多城并行扇出预留。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..retrieval import Weekend
from .timeutil import upcoming_weekend
from .transport import estimate_door_to_door

# DD-08 §5.1 评分权重（活动权重最高 0.30）
WEIGHTS = {
    "act": 0.30, "reach": 0.18, "play": 0.14, "budget": 0.10,
    "local": 0.10, "weather": 0.06, "dining": 0.06, "risk": 0.04, "door": 0.02,
}


def _ev_city(note: str | None = None) -> dict:
    return {"source_type": "open_dataset", "verification_status": "public_source_observed",
            "confidence": 0.7, "note": note}


def _ev_est(note: str | None = None) -> dict:
    return {"source_type": "rule", "verification_status": "estimated", "confidence": 0.4, "note": note}


def _weekend(c: dict) -> Weekend:
    ws, we = c.get("weekend_start"), c.get("weekend_end")
    if ws and we:
        return Weekend(datetime.fromisoformat(ws), datetime.fromisoformat(we))
    sat, sun = upcoming_weekend()
    return Weekend(sat, sun)


def score_city(n_activities: int, d2d_min: int, budget_fit: bool | None,
               play_min: int, risk_level: float = 0.0) -> float:
    """DD-08 §5.1 加权和：活动权重最高；门到门/风险为惩罚。

    诚实标注（未接入数据的维度）：local/weather/dining 为城市档案/运营常量，
    budget 在用户给了预算时因票价/花费数据未接入（DD-09 禁编票价）只能按中性计——
    这些维度的权重（合计 0.32）当前不产生区分度，加权总分是近似排序而非精确评估。
    """
    w = WEIGHTS
    s_act = min(1.0, n_activities / 10.0)
    s_reach = max(0.0, 1.0 - d2d_min / 600.0)
    s_play = min(1.0, play_min / (12 * 60))
    # budget_fit：True=无预算约束（不存在不合）；None=有预算但无法核实 → 中性 0.75；
    # False=确认不合（预留，当前无数据源产出）
    s_budget = 1.0 if budget_fit is True else (0.5 if budget_fit is False else 0.75)
    s_local, s_weather, s_dining = 0.7, 0.8, 0.7  # 城市档案/运营默认（未接入真实数据）
    p_risk = risk_level
    p_door = max(0.0, d2d_min / 600.0)
    return (w["act"] * s_act + w["reach"] * s_reach + w["play"] * s_play + w["budget"] * s_budget
            + w["local"] * s_local + w["weather"] * s_weather + w["dining"] * s_dining
            - w["risk"] * p_risk - w["door"] * p_door)


def _count_activities(session: Session, city_code: str, weekend: Weekend) -> int:
    """当周可选活动计数：与检索层（retrieval/recall.py）同一谓词——
    时间窗重叠（长期展/连载活动当周可去）+ 可信态 + 未过期，
    保证城市卡计数与 bundle 活动列表口径一致（修：卡上 2 场、列表 10 场的矛盾）。"""
    return session.scalar(text(
        "SELECT count(*) FROM activities "
        "WHERE city_code = :c AND verification_status IN ('official_source_confirmed','public_source_observed') "
        "AND start_at <= :e AND COALESCE(end_at, start_at) >= :s AND expires_at > now()"
    ), {"c": city_code, "s": weekend.start, "e": weekend.end}) or 0


def _city_row(session: Session, city_code: str):
    return session.execute(text(
        "SELECT name, ST_X(center::geometry) lng, ST_Y(center::geometry) lat, seasonal_risk "
        "FROM city_playbook WHERE city_code = :c"
    ), {"c": city_code}).first()


def _all_cities(session: Session):
    """全量城市档案（按名称长度倒序，便于子串匹配出发地）。"""
    return session.execute(text(
        "SELECT city_code, name, ST_X(center::geometry) lng, ST_Y(center::geometry) lat "
        "FROM city_playbook ORDER BY length(name) DESC"
    )).all()


def _resolve_origin(origins: list[str] | None, session: Session) -> tuple[dict, str | None]:
    """出发城市解析：origins 文本子串匹配 city_playbook；无/未命中→默认上海（PRD §10 主出发地）。

    返回 ({city_code, name, center}, warning)：兜底上海时 warning 显式声明"按上海处理"
    （不静默），供判同城（C4）与门到门估算（P2-6）。
    """
    cities = _all_cities(session)

    def to_origin(r):
        center = (float(r[2]), float(r[3])) if r[2] is not None else None
        return {"city_code": r[0], "name": r[1], "center": center}

    for o in (origins or []):
        for r in cities:
            if r[1] and r[1] in (o or ""):
                return to_origin(r), None
    # 默认上海（主出发地）——显式标注的兜底，经 warning 上浮
    warning = "未识别出发地城市，按上海处理" if origins else "未提供出发地，按上海处理"
    for r in cities:
        if r[0] == "310000":
            return to_origin(r), warning
    return {"city_code": "310000", "name": "上海", "center": None}, warning


def _budget_fit(budget_band: dict | None) -> bool | None:
    """预算契合判定（用真实 budget_band 计算，不再恒 True）。

    未提供预算 → True（无约束，不存在不合）；提供了预算 → 票价/花费数据未接入
    （DD-09 禁编票价、DD-13 成本块未建），无法核实 → None（评分按中性，不静默声称契合）。
    """
    band = budget_band or {}
    if band.get("min") is None and band.get("max") is None:
        return True
    return None


def _city_card(session: Session, code: str, name: str, center, origin: dict, weekend,
               seasonal_risk=None, budget_band: dict | None = None) -> dict:
    """构建单张城市卡（字段带 evidence）。"""
    n_act = _count_activities(session, code, weekend)
    d2d = estimate_door_to_door(origin.get("center"), center, "rail")
    total_min = d2d.get("total_min", 0)
    play = d2d.get("effective_play_min", 0)
    score = score_city(n_act, total_min, budget_fit=_budget_fit(budget_band), play_min=play)
    return {
        "city_code": code, "name": name, "score": round(score, 3),
        "center": list(center) if center else None,
        "reason": f"当周 {n_act} 场可选活动，门到门约 {total_min} 分钟" if n_act
                  else "城市档案匹配；活动待补搜/可粘贴官方链接",
        "driven_by_activities": {"value": n_act, "evidence": _ev_city(f"当周活动数 {n_act}")},
        "recommended_transport": {"value": f"门到门约 {total_min} 分钟（粗估）",
                                  "evidence": _ev_est("门到门粗估")},
        "effective_play": {"value": play, "evidence": _ev_est("有效游玩粗估")},
        "budget_estimate": {"value": "以官方平台为准", "evidence": _ev_est("票价禁编")},
        "risks": {"value": seasonal_risk or {}, "evidence": _ev_city("季节风险（城市档案）")},
        "evidence": _ev_city("城市档案（运营维护）"),
    }


def destination_discovery(state: dict, session: Session) -> dict:
    """产出 ≤3 张候选城市卡（PRD §04：3 城市对比）+ 出发城市 origin。

    - 指定目标城（非出发城）→ 目标城置首（primary），其余按活动×可达性打分取 top。
    - 目的地留空（DD-07 不再静默默认城市）→ 用解析出的出发地做同城推荐（primary=出发城）。
    - 候选池排除出发城市本身（出发地不作为目的地备选推荐）。
    - 出发地解析兜底上海时，经返回值的 warnings 显式声明（不静默）。
    """
    c = state["constraints"]
    weekend = _weekend(c)
    origin, origin_warning = _resolve_origin(c.get("origins"), session)
    all_cities = _all_cities(session)
    origin_code = origin.get("city_code")
    code = c.get("target_city_code") or origin_code or "310000"  # 留空 → 出发地同城推荐
    budget_band = c.get("budget_band")

    by_code = {r[0]: r for r in all_cities}

    def card_for(r):
        center = (float(r[2]), float(r[3])) if r[2] is not None else None
        sr = session.execute(text(
            "SELECT seasonal_risk FROM city_playbook WHERE city_code = :c"
        ), {"c": r[0]}).scalar()
        return _city_card(session, r[0], r[1], center, origin, weekend,
                          seasonal_risk=sr, budget_band=budget_band)

    # primary：用户指定目标城（档案中有→富卡；无→兜底卡，仍尊重用户选择）
    if code in by_code:
        primary = card_for(by_code[code])
    else:
        primary = {"city_code": code, "name": code, "score": 0.0,
                   "reason": "城市档案暂无，按目标城市处理",
                   "evidence": _ev_est("城市档案不可用")}

    # 备选：按 score 排序，排除出发城与 primary
    scored = []
    for r in all_cities:
        if r[0] == origin_code or r[0] == code:
            continue
        center = (float(r[2]), float(r[3])) if r[2] is not None else None
        n_act = _count_activities(session, r[0], weekend)
        d2d = estimate_door_to_door(origin.get("center"), center, "rail")
        s = score_city(n_act, d2d.get("total_min", 0), _budget_fit(budget_band),
                       d2d.get("effective_play_min", 0))
        scored.append((s, r))
    scored.sort(key=lambda x: x[0], reverse=True)

    cards: list[dict] = []
    if primary:
        cards.append(primary)
    for _, r in scored[: (3 - len(cards))]:
        cards.append(card_for(r))

    if not cards:  # 城市档案全空兜底
        cards = [{"city_code": code, "name": code, "reason": "城市档案暂无，按目标城市处理",
                  "evidence": _ev_est("城市档案不可用")}]

    out: dict = {"candidate_cities": cards[:3], "origin": origin}
    if origin_warning:
        out["warnings"] = [origin_warning]
    return out
