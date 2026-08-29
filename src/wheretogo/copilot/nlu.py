"""Stable travel-attribute parsing.

Open-ended experience requirements are interpreted with conversation context
by ``copilot.interpreter``.  This module intentionally has no interest or
preference taxonomy.
"""
from __future__ import annotations

import re
from datetime import datetime as _dt, timedelta

from ..config import SHANGHAI_TZ
from ..providers import extract_json

def normalize_interests(values) -> list[str]:
    """Legacy compatibility: keep arbitrary requirement text verbatim."""
    raw_values = values if isinstance(values, list) else [values]
    return list(dict.fromkeys(
        str(raw or "").strip()
        for raw in raw_values
        if str(raw or "").strip()
    ))


def interest_mentions(text: str) -> list[tuple[int, str]]:
    """Deprecated: an open concept cannot be found from a closed lexicon."""
    return []


def normalize_soft_preferences(values) -> list[str]:
    """Legacy compatibility: keep arbitrary preference text verbatim."""
    return normalize_interests(values)


def soft_preferences_from_text(text: str) -> list[str]:
    """Open preferences require the contextual interpreter; rules do not guess."""
    return []

# ═══════════════════════════════════════════════════════════════════════
# 公共入口
# ═══════════════════════════════════════════════════════════════════════


def extract_constraints_from_text(text: str, use_llm: bool = True,
                                  memory_ctx: dict | None = None,
                                  history: list[dict] | None = None) -> dict:
    """从自然语言抽取约束。LLM 优先（语义理解一切），规则纯兜底。

    memory_ctx（此前已记下的约束）与 history（近期对话）会注入 LLM prompt 作为
    对话上下文——槽位填充类回答（"上海"）与指代/省略句必须知道上一轮在问什么
    才能填对槽位；规则兜底路径不消费二者（上下文纠偏在 handle_turn 确定性完成）。
    """
    t = (text or "").strip()
    if not t:
        return {}
    if use_llm:
        result = _llm_extract(t, memory_ctx=memory_ctx, history=history)
        if result:  # LLM 成功 → 直接返回（权威）
            deterministic_soft = soft_preferences_from_text(t)
            if deterministic_soft:
                result["soft_preferences"] = list(dict.fromkeys(
                    (result.get("soft_preferences") or []) + deterministic_soft
                ))
            return result
    # LLM 不可用/失败 → 规则降级
    return _rule_extract(t)


# ═══════════════════════════════════════════════════════════════════════
# LLM 主路径
# ═══════════════════════════════════════════════════════════════════════


def _context_block(memory_ctx: dict | None, history: list[dict] | None = None) -> str:
    """把已记下约束 + 近期对话 + 上一轮未决槽位渲染为 prompt 上下文块（都空→空串）。"""
    if not memory_ctx and not history:
        return ""
    lines: list[str] = []
    if memory_ctx:
        target = memory_ctx.get("target_city_name") or "未定"
        origins = "、".join(memory_ctx.get("origins") or []) or "未填写"
        ws = str(memory_ctx.get("weekend_start") or "")[:10] or "未定"
        we = str(memory_ctx.get("weekend_end") or "")[:10] or ""
        interests = "、".join(memory_ctx.get("interests") or []) or "未填写"
        lines += [
            "对话上下文（此前几轮已记下的约束）：",
            f"  目的地={target}；出发地={origins}；周末={ws}~{we}；兴趣={interests}",
        ]
        if not memory_ctx.get("origins"):
            lines.append(
                "上一轮正在追问「出发地」：若用户本轮是在回答该问题（如只回了一个"
                "城市名/区域名），必须把该城市填入 origins，而不是 target_city_name。"
            )
    if history:
        lines.append("近期对话（旧→新，据此理解指代/省略/追问回答）：")
        for turn in history[-6:]:
            role = "用户" if turn.get("role") == "user" else "AI"
            content = str(turn.get("content") or "")[:80]
            lines.append(f"  {role}：{content}")
    return "\n".join(lines) + "\n\n"


def _llm_extract(text: str, memory_ctx: dict | None = None,
                 history: list[dict] | None = None) -> dict:
    """大模型语义理解：解析任意语言、任意格式的出行约束。"""
    now_str = _dt.now(SHANGHAI_TZ).strftime("%Y-%m-%d %A")
    parsed = extract_json(
        "constraint_parse",
        _context_block(memory_ctx, history)
        + f"""当前日期：{now_str}。从用户消息中抽取周末出行约束，输出 JSON（缺失的键不要写）。

字段定义与示例：
- origins: list[str] — 出发城市。"从深圳出发"→["深圳"]，"我在成都"→["成都"]，"坐标杭州"→["杭州"]
- target_city_name: str — 目的地城市名。"去杭州"→"杭州"，"想去大理"→"大理"。若只说"想去海边"等非具体城市则不填
- party_size: int — 人数。"两个人"→2，"一家三口"→3，"我和闺蜜"→2，"带娃"→2，"couple"→2
- budget_max: int — 人均预算上限(元)。"预算两千"→2000，"穷游"→500，"经济型"→800。模糊/不差钱则不填
- experience_requirements: list[str] — 原样保留用户对体验的正向要求、排除项和偏好，
  不映射到预定义类别。例如用户表达的“适合两人慢慢逛”“不要室内场馆”都保留为开放文本
- research_goal: str — 用完整自然语言概括用户本轮真正想解决的研究问题
- acceptance_criteria: list[str] — 候选需要逐项满足或核实的开放文本标准
- dietary: list[str] — 忌口/饮食限制。"不吃辣"→["辣"]，"我吃素"→["素食"]，"清真"→["清真"]，"过敏花生"→["花生"]
- departure_date: str(ISO日期) — 出行开始日期。解析任意语言的时间表达为具体日期：
  "today/今天"→当天，"tomorrow/明天"→明天，"后天"→后天，"3天后/8天后/10天后"→当前日期+N天，
  "下周六"→下周六具体日期，"this weekend"→本周五，"next weekend"→下周五，
  "8月3号"→2026-08-03，"五一"→2027-05-01，"国庆节"→2026-10-01。若无时间信息则不填
- trip_end_date: str(ISO日期) — 出行结束日期。LLM根据语义自行判断合理时长：
  "周末"→周日，"五一"→05-05，"国庆"→10-07，"圣诞节"→12-26，"清明节"→对应放假末日，
  单日出行→同一天。用户没明确说则按合理推断。若无时间信息则不填
- latest_return: str(ISO日期时间) — 最晚返回时间。"周日晚上回"→该周日20:00""",
        text,
    )
    if not isinstance(parsed, dict):
        return {}
    return _normalize_llm_result(parsed)


def _normalize_llm_result(parsed: dict) -> dict:
    """将 LLM 输出标准化为内部格式。"""
    out: dict = {}
    # origins
    if parsed.get("origins"):
        origins = parsed["origins"]
        out["origins"] = origins if isinstance(origins, list) else [str(origins)]
    # target
    if parsed.get("target_city_name"):
        out["target_city_name"] = str(parsed["target_city_name"]).strip()
    # party_size
    if parsed.get("party_size"):
        try:
            out["party_size"] = int(parsed["party_size"])
        except (TypeError, ValueError):
            pass
    # budget
    if parsed.get("budget_max"):
        try:
            out["budget_band"] = {"max": int(parsed["budget_max"])}
        except (TypeError, ValueError):
            pass
    for field in ("experience_requirements", "acceptance_criteria"):
        if parsed.get(field):
            out[field] = normalize_interests(parsed[field])
    if parsed.get("research_goal"):
        out["research_goal"] = str(parsed["research_goal"]).strip()
    # Read old provider outputs without canonicalizing their values.
    if parsed.get("interests"):
        out["interests"] = normalize_interests(parsed["interests"])
    if parsed.get("soft_preferences"):
        out["soft_preferences"] = normalize_soft_preferences(parsed["soft_preferences"])
    # dietary
    if parsed.get("dietary"):
        diet = parsed["dietary"]
        out["dietary"] = diet if isinstance(diet, list) else [str(diet)]
    # departure_date + trip_end_date → weekend_start/end（LLM决定窗口，零硬编码）
    if parsed.get("departure_date"):
        try:
            dep = _dt.fromisoformat(str(parsed["departure_date"]))
            if dep.tzinfo is None:
                dep = dep.replace(tzinfo=SHANGHAI_TZ)
            day = dep.replace(hour=0, minute=0, second=0, microsecond=0)
            out["weekend_start"] = day.isoformat()
            if parsed.get("trip_end_date"):
                try:
                    end = _dt.fromisoformat(str(parsed["trip_end_date"]))
                    if end.tzinfo is None:
                        end = end.replace(tzinfo=SHANGHAI_TZ)
                    end_day = end.replace(hour=23, minute=59, second=59, microsecond=0)
                    out["weekend_end"] = end_day.isoformat()
                except (ValueError, TypeError):
                    out["weekend_end"] = (day + timedelta(days=2)).isoformat()
            else:
                out["weekend_end"] = (day + timedelta(days=2)).isoformat()
        except (ValueError, TypeError):
            pass
    # latest_return
    if parsed.get("latest_return"):
        out["latest_return"] = str(parsed["latest_return"])
    return out


# ═══════════════════════════════════════════════════════════════════════
# 规则降级（use_llm=False 时的离线兜底）
# ═══════════════════════════════════════════════════════════════════════

_CITY_HINTS = [
    "上海", "北京", "杭州", "苏州", "南京", "成都", "重庆", "西安", "武汉", "长沙",
    "广州", "深圳", "厦门", "青岛", "天津", "宁波", "无锡", "黄山",
]
_NUM_MAP = {"两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8}


def _rule_extract(t: str) -> dict:
    """硬编码规则抽取（离线兜底）。"""
    out: dict = {}
    # 出发地
    origins: list[str] = []
    m = re.search(r"从([一-龥]{2,6}?)(?:出发|动身|去|到|起)", t)
    if m:
        origins.append(m.group(1))
    for c in _CITY_HINTS:
        if c in t and c not in origins and (f"从{c}" in t or f"{c}出发" in t or f"{c}起" in t):
            origins.append(c)
    # 常见省略表达：“上海去杭州 / 上海到杭州 / 上海→杭州”。
    # “A 去 B 还是 B 去 A、还没想好”属于未决问题，不能擅自选其中一条。
    route_is_ambiguous = (
        "还是" in t
        and sum(1 for city in _CITY_HINTS if city in t) >= 2
    ) or any(marker in t for marker in ("路线没想好", "方向没想好", "还没想好", "尚未决定"))
    route_match: tuple[str, str] | None = None
    if not route_is_ambiguous:
        for origin in sorted(_CITY_HINTS, key=len, reverse=True):
            for target in sorted(_CITY_HINTS, key=len, reverse=True):
                if origin == target:
                    continue
                if re.search(
                    rf"{re.escape(origin)}\s*(?:去|到|→|->|—|-)\s*{re.escape(target)}",
                    t,
                ):
                    route_match = (origin, target)
                    break
            if route_match:
                break
    if route_match and route_match[0] not in origins:
        origins.append(route_match[0])
    if origins:
        out["origins"] = origins
    # 人数
    pm = re.search(r"(\d|两|二|三|四|五|六|七|八)\s*个?\s*人", t)
    if pm:
        party_size = _NUM_MAP.get(pm.group(1)) or int(pm.group(1))
        if party_size > 0:
            out["party_size"] = party_size
    elif re.search(r"一家三口", t):
        out["party_size"] = 3
    elif re.search(r"一家四口", t):
        out["party_size"] = 4
    elif re.search(r"一个人|独自|solo\b", t, re.IGNORECASE):
        out["party_size"] = 1
    elif re.search(r"情侣|两口子|我和(?:朋友|闺蜜|对象|老婆|老公|孩子)|带娃", t):
        out["party_size"] = 2
    # 预算
    bm = re.search(r"(?:预算|人均|每人|每个人)[^0-9]{0,4}(\d{2,5})", t)
    if bm:
        out["budget_band"] = {"max": int(bm.group(1))}
    # 兴趣
    ints = [canonical for _pos, canonical in interest_mentions(t)]
    if ints:
        out["interests"] = list(dict.fromkeys(ints))
    soft_preferences = soft_preferences_from_text(t)
    if soft_preferences:
        out["soft_preferences"] = soft_preferences
    # 忌口
    diet: list[str] = []
    for neg in ["不吃辣", "忌辣", "无辣", "不要辣"]:
        if neg in t and "辣" not in diet:
            diet.append("辣")
    for neg in ["不吃香菜", "不吃笋", "不吃内脏", "不吃牛", "不吃海鲜"]:
        if neg in t:
            diet.append(neg.replace("不吃", ""))
    if diet:
        out["dietary"] = list(dict.fromkeys(diet))
    # 目的地：显式“改/换”优先；多个“去”表达取最后一个未被否定的城市。
    # 这样“别去杭州，改苏州”不会把被拒绝的杭州重新写回。
    if not route_is_ambiguous:
        explicit_targets: list[tuple[int, str]] = []
        positive_targets: list[tuple[int, str]] = []
        for city in sorted(_CITY_HINTS, key=len, reverse=True):
            for match in re.finditer(
                rf"(?:改(?:成|为|到)?|换(?:成|为|到|去|个)?)\s*{re.escape(city)}",
                t,
            ):
                explicit_targets.append((match.start(), city))
            for match in re.finditer(rf"(?:去|到)\s*{re.escape(city)}", t):
                prefix = t[max(0, match.start() - 3):match.start()]
                if prefix.endswith(("不", "别", "不要")):
                    continue
                positive_targets.append((match.start(), city))
            direct = re.search(
                rf"(?:目的地|终点)\s*(?:是|定在|选|：|:)?\s*{re.escape(city)}",
                t,
            )
            if direct:
                explicit_targets.append((direct.start(), city))
        candidates = explicit_targets or positive_targets
        if candidates:
            out["target_city_name"] = max(candidates, key=lambda item: item[0])[1]
    if route_match:
        out["target_city_name"] = route_match[1]
    # 相对日期
    for _word, _off in (("the day after tomorrow", 2), ("day after tomorrow", 2),
                         ("tomorrow", 1), ("tonight", 0), ("today", 0),
                         ("大后天", 3), ("后天", 2), ("明天", 1), ("今晚", 0), ("今天", 0)):
        if _word in t.lower():
            _day = (_dt.now(SHANGHAI_TZ) + timedelta(days=_off)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            out["weekend_start"] = _day.isoformat()
            out["weekend_end"] = (_day + timedelta(days=2)).isoformat()
            break
    # 相对周末
    if not out.get("weekend_start"):
        mw = re.search(r"next weekend|下下周末|下下个?周|下周末|下个?周|this weekend|这周末|本周末|这周|周末|周五|周六|周日", t, re.IGNORECASE)
        if mw:
            from ..domain.timeutil import upcoming_weekend
            sat, sun = upcoming_weekend()
            off = 14 if mw.group(0).startswith("下下") else (7 if (mw.group(0).startswith("下") or "next" in mw.group(0).lower()) else 0)
            if off:
                sat, sun = sat + timedelta(days=off), sun + timedelta(days=off)
            out["weekend_start"] = sat.isoformat()
            out["weekend_end"] = sun.isoformat()
    # 裸城市词兜底：整句（剥掉语气词后）恰好是一个城市名——通常是用户在回答追问
    # （如"你们从哪里出发？"→"上海"）。这里先按目的地候选抽出，由 handle_turn
    # 依据对话上下文纠偏：上一轮缺出发地且已有目的地时改判为 origins。
    if not out.get("target_city_name") and not out.get("origins"):
        bare = re.sub(r"[吧呀啊呢嘛哈~！!。.\s]+$", "", t)
        bare = re.sub(r"^(?:就[是说]?|还是|选)", "", bare)
        if bare in _CITY_HINTS:
            out["target_city_name"] = bare
    return out
