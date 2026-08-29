"""DD-15 对话式规划 Copilot：意图分类 + **最小可用闭环**（不只是路由标签）。

闭环动作（v2 D1：chat-first 真多轮；LLM 不直接操作图，确定性路由）：
- provide_constraints/clarify_answer：从消息抽约束（NLU）→ 返回 constraints_patch + 自然回复 + 仍缺的追问
- refine_field：解析要改的字（如目的地）→ constraints_patch
- confirm_booking：本地正则抽车次/航班/酒店 → 返回 booking 草稿
- ask_info：查库内活动 + 官方源 → 真回答
- chitchat：固定回复
BFF 负责：加载既有约束（memory_ctx，不反复追问）、落库 constraints_patch、按 booking 触发 resume。
"""
from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..domain.constraints import missing_slots
from ..providers import extract_json
from ..providers.llm import chat
from .interpreter import interpret_turn

# 意图 → (LangGraph 驱动方式, 说明)
ROUTE_TABLE: dict[str, tuple[str, str]] = {
    "provide_constraints": ("invoke", "抽取约束并入图"),
    "clarify_answer": ("invoke", "多轮澄清"),
    "refine_field": ("update_state", "局部细化"),
    "deep_research": ("invoke", "触发深搜"),
    "confirm_booking": ("resume", "回填确认"),
    "ask_info": ("answer", "库内问答"),
    "design_itinerary": ("answer", "锚点路线设计"),
    "weather": ("replan", "天气重规划"),
    "chitchat": ("answer", "闲聊"),
}

_VALID = set(ROUTE_TABLE)
_GREETING = "你好～告诉我这个周末想从哪出发、几个人、预算和兴趣，我帮你找当周活动和交通方案。"


def _looks_like_explicit_refinement(msg: str) -> bool:
    """Generic offline hint for a state-changing correction.

    It recognizes the operation language, never the user's topic.
    """
    value = (msg or "").lower()
    negative_view = re.sub(
        r"(?:并不是|不是|并非)\s*(?:真的)?\s*不(?=喜欢|想看|想逛)",
        "",
        value,
    )
    field_change = (
        ("目的地", "改"), ("目的地", "换"), ("预算", "改"), ("预算", "调"),
        ("出发地", "改"), ("出发地", "换"), ("日期", "改"), ("日期", "换"),
        ("时间", "改"), ("时间", "换"), ("周末", "改"), ("周末", "换"),
    )
    if any(a in value and b in value for a, b in field_change):
        return True
    if any(marker in negative_view for marker in (
        "改看", "改成", "改为", "换成", "换看", "不想看", "不想逛", "不要看", "不看",
        "不考虑", "排除", "除了", "别推荐", "不要推荐", "不推荐", "别安排", "不要安排",
    )):
        return True
    if "不喜欢" in negative_view:
        points_to_results = any(marker in negative_view for marker in (
            "这些", "这几个", "这一批", "这批", "上面", "刚才", "推荐的",
            "再找", "再搜", "其他", "别的", "更多", "还有",
        ))
        if not points_to_results:
            return True
    return False


def _looks_like_booking(msg: str) -> bool:
    value = (msg or "").strip().lower()
    if any(q in value for q in ("多少钱", "余票", "还有票", "几点", "什么时候", "怎么")):
        return False
    # 宁可让用户再次确认，也不能把“还没买/只是考虑”误写成已购订单。
    if any(denial in value for denial in (
        "没买", "没有买", "还没买", "未买", "没订", "没有订", "还没订", "未订",
        "不是说我买", "不是已经买", "只是看看", "只是考虑", "只是问",
    )):
        return False
    if any(marker in value for marker in (
        "买好", "买了", "订好", "订了", "已订", "订单", "回填", "入住",
    )):
        return True
    return bool(
        re.search(r"\b[GDCZTK]\d{1,5}\s*次?\b", value, re.IGNORECASE)
        or re.search(r"\b(?:[A-Z]{2}\d{3,4})\b", value, re.IGNORECASE)
    )


def _rule_intent(msg: str) -> str | None:
    """Conservative outage fallback over control speech acts, not topic words."""
    if _looks_like_booking(msg):
        return "confirm_booking"
    if _looks_like_route_design(msg):
        return "design_itinerary"
    if _looks_like_explicit_refinement(msg):
        return "refine_field"
    if re.match(r"^\s*(你好|谢谢|嗨|hello\b|hi\b|再见|哈喽)", msg.strip().lower()):
        return "chitchat"
    if re.search(r"(天气|下雨|暴雨|台风|下雪)", msg):
        return "weather"
    if re.search(r"(多少钱|几点|地址|在哪|余票|还有票)", msg):
        return "ask_info"
    if re.search(r"(不要|不想|排除|除了)", msg):
        return "refine_field"
    if re.search(
        r"((再|继续|重新).{0,4}(搜|找|推荐)|"
        r"(还有没有|有没有|其他|别的).{0,10}(吗|推荐|选择|去处|地方)?|换个|最新)",
        msg,
    ):
        return "deep_research"
    return "provide_constraints"


def _looks_like_route_design(msg: str) -> bool:
    """高置信"点名锚点/要求排路线"（先于 LLM 与约束抽取，避免被当普通兴趣重跑推荐）。"""
    value = msg or ""
    if re.search(r"(?:设计|规划|安排|排)\s*[^，。]{0,8}(?:路线|行程|一日|两日|攻略)", value):
        return True
    if re.search(r"(?:路线|行程)\s*(?:怎么|如何|帮忙|帮我)", value):
        return True
    # 「既要 A 也要 B」多锚点显式结构
    if "既要" in value and "也要" in value:
        return True
    return False


def classify_intent(message: str, use_llm: bool = True) -> str:
    """意图分类。LLM 优先（语义理解），规则纯兜底。"""
    msg = (message or "").lower()
    if _looks_like_booking(msg):
        return "confirm_booking"
    if _looks_like_route_design(msg):
        return "design_itinerary"
    if _looks_like_explicit_refinement(msg):
        return "refine_field"
    if use_llm:
        out = chat("intent_classify", [
            {"role": "system", "content": (
                "把用户消息分到一个意图标签，只输出标签、不要解释。可选：" + "/".join(sorted(_VALID)) + "。"
                "指南：给出发地/目的地/人数/预算/兴趣/日期/时间等出行信息→provide_constraints；"
                "只要是问具体信息（价格/时间/地点/详情/怎么去），即使句中同时含有出行约束（城市/活动类型）也→ask_info——"
                "示例：'万兽之王演唱会门票多少钱'→ask_info，不要当成 provide_constraints；"
                "说已买好票/订好酒店/贴订单→confirm_booking；"
                "点名具体场馆/活动并要求排路线/行程/一日安排（如'既要去A也要去B，帮我设计一条路线'）→design_itinerary；"
                "用户主动声明要修改约束字段（如'目的地改成XX'/'预算调到XX'/'不看展览了改看演出'）→refine_field；"
                "提到天气恶劣/是否调整行程→weather；"
                "纯打招呼寒暄(hello/你好/谢谢)→chitchat；"
                "严格区分 deep_research vs refine_field："
                "用户对当前已推荐结果不满意/想要更多/想换/觉得不够好/追问有没有别的/再找找/还有吗→deep_research；"
                "示例：'这几个我不喜欢还有别的吗'→deep_research；'目的地换成苏州'→refine_field。"
                "要求搜索最新信息/查一下→deep_research。"
            )},
            {"role": "user", "content": message or ""},
        ])
        if out:
            label = out.strip().split()[0].strip(",。.") if out.strip() else ""
            if label in _VALID:
                return label  # LLM 结果即权威，不被规则覆盖
    # LLM 不可用/失败 → 规则降级
    return _rule_intent(msg) or "provide_constraints"


def _iso_dt(value):
    """ISO 字符串 → datetime（None/非法 → None）。"""
    if not value:
        return None
    try:
        from datetime import datetime as _dtd
        return _dtd.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _extract_anchor_names(message: str, memory_ctx: dict, use_llm: bool = True) -> list[str]:
    """LLM 抽取用户点名的具体场馆/活动名（非活动类型）；离线返回空由库内扫描兜底。"""
    if not use_llm:
        return []
    parsed = extract_json(
        "anchor_extract",
        "从用户消息中抽取其点名要去的**具体**场馆/活动/演出名称（不是活动类型，是具体名字）。"
        "输出 JSON {anchors:[str..]}。例：「既要去动漫博物馆，也要去万兽之王巡回演唱会，帮我设计一条路线」"
        "→ {\"anchors\":[\"动漫博物馆\",\"万兽之王巡回演唱会\"]}；只有类型没有具体名字（如「想看展」）"
        "→ {\"anchors\":[]}",
        message,
    )
    if isinstance(parsed, dict) and isinstance(parsed.get("anchors"), list):
        return [str(a).strip() for a in parsed["anchors"] if str(a).strip()]
    return []


def _scan_anchor_names(message: str, session: Session, city_code: str | None,
                       ws, we) -> list[str]:
    """离线锚点兜底：库内可信未过期活动中，venue 被消息提及（≥4 字）或与 title
    最长公共子串 ≥5 字者，视为用户点名。"""
    from ..domain.route_design import _longest_common, _norm
    from ..enums import TRUSTED_STATUSES
    from ..models import Activity

    q = session.query(Activity).filter(
        Activity.verification_status.in_(list(TRUSTED_STATUSES)),
        Activity.expires_at > ws,
        Activity.start_at <= we,
    )
    if city_code:
        q = q.filter(Activity.city_code == city_code)
    rows = q.limit(300).all()
    msg_norm = _norm(message)
    names: list[str] = []
    for row in rows:
        venue = _norm(row.venue or "")
        title = (row.title or "").strip()
        if venue and len(venue) >= 4 and venue in msg_norm:
            names.append((row.venue or "").strip())
        elif title and _longest_common(message, title) >= 5:
            names.append(title)
    return names


def _summarize(c: dict, changed: set[str] | None = None) -> str:
    parts = []
    if c.get("origins"):
        parts.append("从" + "/".join(c["origins"]) + "出发")
    if c.get("party_size"):
        parts.append(f"{c['party_size']}人")
    if c.get("budget_band"):
        parts.append(f"预算≤{c['budget_band'].get('max')}")
    if c.get("interests"):
        parts.append("想玩" + "/".join(c["interests"]))
    if c.get("experience_requirements"):
        parts.append("希望" + "/".join(c["experience_requirements"]))
    if c.get("soft_preferences"):
        parts.append("偏好" + "/".join(c["soft_preferences"]))
    if c.get("dietary"):
        parts.append("忌" + "/".join(c["dietary"]))
    if c.get("target_city_name"):
        # 历史回显用中性措辞；仅本轮真正变更的字段才说"改"（修：每轮重复"目的地改X"）
        if changed is None or "target_city_name" in changed:
            parts.append(f"目的地改{c['target_city_name']}")
        else:
            parts.append(f"目的地{c['target_city_name']}")
    if c.get("weekend_start"):
        parts.append("周末" + str(c["weekend_start"])[5:10])
    return "；".join(parts) if parts else ""


def _summarize_booking(ex: dict) -> str:
    bits = []
    for k in ("train_no", "flight_no", "name"):
        if ex.get(k):
            bits.append(ex[k])
    if ex.get("from_station") and ex.get("to_station"):
        bits.append(f"{ex['from_station']}→{ex['to_station']}")
    if ex.get("date"):
        bits.append(ex["date"])
    return " ".join(bits)


_QA_STOP = set("那个这个哪儿哪里门票多少钱什么怎么为什么几点地址在哪的了吗呢啊吧要不要调整办呀我你他她想请问一下儿")


def _qa_keywords(message: str, use_llm: bool = True) -> list[str]:
    """从问句抽取查询关键词：LLM 抽实体优先（如‘莫奈’）；无 LLM→去停用词后 2 字滑窗。"""
    kws: list[str] = []
    if use_llm:
        parsed = extract_json(
            "qa_keyword",
            "提取用户想查询的活动/展览/演出名称关键词（如‘莫奈’），输出 JSON {keywords:[...]}；不要含疑问词",
            message or "",
        )
        if isinstance(parsed, dict) and parsed.get("keywords"):
            kws = [str(k).strip() for k in parsed["keywords"] if str(k).strip()]
    if not kws:
        chars = "".join(ch for ch in (message or "") if "一" <= ch <= "鿿" and ch not in _QA_STOP)
        kws = list(dict.fromkeys(chars[i:i + 2] for i in range(max(0, len(chars) - 1)))) or ["展览"]
    return kws[:5]


def _answer_from_db(message: str, session: Session | None, city_code: str | None,
                    use_llm: bool = True) -> str:
    """ask_info：检索库内活动（关键词 OR 匹配，修 C4）→ LLM 结合问题生成自然语言回答；
    无 LLM → 关键词匹配模板拼接并显式标注降级；无数据→提示粘贴官方链接（§09 韧性）。"""
    if not session:
        return "我查一下——你可以把活动官方链接发我，我帮你核实后汇入。"
    kws = _qa_keywords(message, use_llm=use_llm)
    clauses = " OR ".join(f"title ILIKE :kw{i}" for i in range(len(kws)))
    params: dict = {f"kw{i}": f"%{k}%" for i, k in enumerate(kws)}
    city_clause = ""
    if city_code:  # 仅在有城市码时加过滤（避免 NULL 参数导致 AmbiguousParameter 报错）
        city_clause = " AND city_code = :city"
        params["city"] = city_code
    try:
        rows = session.execute(text(
            f"SELECT title, price_text, booking_url, verification_status "
            f"FROM activities WHERE ({clauses}) AND verification_status IN "
            f"('official_source_confirmed','public_source_observed') "
            f"AND (expires_at IS NULL OR expires_at > now()){city_clause} "
            f"ORDER BY start_at LIMIT 3"
        ), params).all()
    except Exception:  # DB 异常不外抛（ask_info 不崩），降级为引导粘贴链接
        return "我查一下——把活动官方链接发我，我核实后汇入计划。"
    if not rows:
        return (f"库内暂无「{'/'.join(kws)}」相关的活动记录。你可以把官方页面链接发我，"
                "我核实后汇入计划（证据优先）。")
    if use_llm:
        answer = _llm_answer(message, rows)
        if answer:
            return answer
    # 降级（无 LLM key/调用失败）：关键词匹配模板拼接，显式标注不静默
    lines = []
    for r in rows:
        price = r[1] or "价格见官方"
        tag = "官方确认" if r[3] == "official_source_confirmed" else "公开来源"
        lines.append(f"· {r[0]}（{price}，{tag}）")
    return ("（当前为关键词匹配结果，非智能问答）找到这些：\n" + "\n".join(lines)
            + "\n价格/余票以官方页面为准。")


def _llm_answer(message: str, rows) -> str | None:
    """候选行交 LLM 生成自然语言回答；无 key/失败返回 None（调用方走标注降级）。

    系统提示红线：事实字段只允许引用行内数据；记录没有的信息明说没有并给官方入口。
    """
    catalog = [
        {"title": r[0], "price_text": r[1], "booking_url": r[2],
         "verification_status": "官方确认" if r[3] == "official_source_confirmed" else "公开来源"}
        for r in rows
    ]
    return chat("qa_answer", [
        {"role": "system", "content": (
            "你是周末出行助手的问答员，根据给定的「库内活动记录」用中文简洁回答用户问题。"
            "红线：①活动名/价格/时间/链接等事实只允许引用记录内的数据，禁止编造或凭常识补充；"
            "②记录里没有的信息就明说「库内暂时没有」，并引导用户发活动官方页面链接以便核实汇入；"
            "③结尾提醒价格/余票以官方页面为准。"
        )},
        {"role": "user", "content": f"用户问题：{message}\n库内活动记录：{catalog}"},
    ])


def _apply_city_code(extracted: dict, session: Session | None) -> None:
    """目的地名 → target_city_code（planner 只读 code，修 C3）；并确保目的地不被当作出发地。"""
    name = extracted.get("target_city_name")
    if not name:
        return
    if session is not None:
        try:
            code = session.scalar(text(
                "SELECT city_code FROM city_playbook WHERE name = :n OR :n LIKE '%' || name || '%' "
                "ORDER BY length(name) DESC LIMIT 1"
            ), {"n": name})
        except Exception:
            code = None
        if code:
            extracted["target_city_code"] = code
    if extracted.get("origins"):
        extracted["origins"] = [o for o in extracted["origins"] if name not in (o or "") and (o or "") not in name]
        if not extracted["origins"]:
            extracted.pop("origins")


def _has_trip_constraints(c: dict) -> bool:
    """消息是否含实质出行约束（用于纠偏被 LLM 误判为问答/闲聊/深搜的约束消息）。"""
    return bool(c) and any(
        c.get(k) for k in (
            "origins", "target_city_name", "target_city_code",
            "experience_requirements", "research_goal", "interests",
            "party_size", "budget_band", "weekend_start",
        )
    )


_QUESTION_MARKERS = (
    "多少", "什么", "几点", "在哪", "哪里", "哪儿", "地址", "怎么去",
    "吗", "呢", "？", "?",
)


def _looks_like_question(message: str) -> bool:
    """是否疑问句——疑问句不被约束纠偏劫持（保住 ask_info 的回答职责）。"""
    return any(m in (message or "") for m in _QUESTION_MARKERS)


def _refined_interests(message: str, extracted: dict, memory_ctx: dict) -> dict:
    """Deprecated compatibility hook; contextual interpretation is authoritative."""
    return extracted


def build_route_plan(
    user_message: str,
    memory_ctx: dict,
    session: Session | None,
    *,
    city_code: str | None = None,
    use_llm: bool = True,
    conversation: list[dict] | None = None,
    extracted: dict | None = None,
) -> tuple[str, dict | None, dict]:
    """design_itinerary 核心：抽约束→锚点解析→排路线（供旧 chat 与 v4 turn 共用）。

    返回 (reply, route_plan|None, c_extracted)；锚点全部没听出时 route_plan=None。
    """
    from datetime import datetime as _dt3, timedelta as _td3

    from ..config import SHANGHAI_TZ as _TZ
    from ..domain.route_design import design_day_route, resolve_anchors
    from .nlu import extract_constraints_from_text

    # 消息自身携带的约束（目的地/出发地/周末）必须先抽取并合并——
    # 否则锚点匹配用错城市/时间窗（实测："下周末从杭州出发"被按"今天、无城市过滤"匹配）
    c_extracted = extract_constraints_from_text(
        user_message, use_llm=use_llm, memory_ctx=memory_ctx,
        history=list(conversation or []))
    if extracted:
        c_extracted = {**extracted, **c_extracted}
    _apply_city_code(c_extracted, session)
    base = {**(memory_ctx or {}), **c_extracted}

    names = _extract_anchor_names(user_message, memory_ctx or {}, use_llm=use_llm)
    code = base.get("target_city_code") or city_code
    if not code and base.get("origins") and session is not None:
        # 目的地留空 → 按出发地同城（与 discover 的语义一致）
        code = session.scalar(text(
            "SELECT city_code FROM city_playbook WHERE name = :n OR :n LIKE '%' || name || '%' "
            "ORDER BY length(name) DESC LIMIT 1"
        ), {"n": base["origins"][0]})
    ws = _iso_dt(base.get("weekend_start")) or _dt3.now(_TZ)
    we = _iso_dt(base.get("weekend_end")) or (ws + _td3(days=2))
    resolved: list[dict] = []
    pending: list[str] = []
    if session is not None:
        if not names:  # 离线/LLM 未抽出 → 扫描库内可信活动做子串匹配兜底
            names = _scan_anchor_names(user_message, session, code, ws, we)
        if names:
            resolved, pending = resolve_anchors(names, session, code, ws, we)
    else:
        pending = names
    if not resolved and not pending:
        reply = ("想帮你排路线，但没听出具体要去哪几个地方——"
                 "直接点名场馆/活动就行，比如「既要去动漫博物馆，也要去看万兽之王」。")
        return reply, None, c_extracted
    route_plan = design_day_route(resolved, pending, ws, we)
    n = len(resolved) + len(pending)
    reply = (f"按你点名的 {n} 个锚点排好路线了（场次/接驳时间为规划估算，"
             "以官方页面或票面为准），见下方路线卡。")
    return reply, route_plan, c_extracted


def handle_turn(
    plan_id: str,
    user_message: str,
    memory_ctx: dict | None = None,
    use_llm: bool = True,
    session: Session | None = None,
    city_code: str | None = None,
    *,
    conversation: list[dict] | None = None,
    stage: str | None = None,
    pending_clarify_ctx: list[dict] | None = None,
    latest_results: dict | None = None,
) -> dict:
    """解释并处理一轮对话，同时保留旧版 ``intent/action`` 响应契约。

    ``TurnDecision`` 的 acts/commands 才是新架构的权威语义；旧字段用于渐进兼容。
    """
    memory_ctx = memory_ctx or {}
    fallback_intent = (
        "confirm_booking" if _looks_like_booking(user_message)
        else "design_itinerary" if _looks_like_route_design(user_message)
        else "refine_field" if _looks_like_explicit_refinement(user_message)
        else _rule_intent((user_message or "").lower()) or "provide_constraints"
    )
    interpreted = interpret_turn(
        user_message,
        fallback_intent=fallback_intent,
        memory_ctx=memory_ctx,
        conversation=conversation,
        stage=stage,
        pending_clarify=pending_clarify_ctx,
        latest_results=latest_results,
        use_llm=use_llm,
    )
    semantic_primary_intent = interpreted.primary_intent
    intent = semantic_primary_intent
    extracted = dict(interpreted.constraints_patch)
    _apply_city_code(extracted, session)
    acts = list(interpreted.acts)
    model_reply = str(interpreted.assistant_reply or "").strip()
    if extracted and "update_constraints" not in acts:
        acts.insert(0, "update_constraints")
    # 高置信"点名锚点/要求排路线"（DD-15 v1.1）：interpreter 意图表不含
    # design_itinerary，由确定性规则接管，避免被当普通兴趣重跑推荐。
    if _looks_like_route_design(user_message or ""):
        intent = "design_itinerary"
        semantic_primary_intent = "design_itinerary"
    # 兼容旧客户端：闲聊/问答里带明确约束时，legacy intent 仍以录入约束为主；
    # 新契约通过 acts 保留 answer_info，TurnDecision 也保留语义主意图。
    # 但纯问句（仅软约束 + 疑问词，如"门票多少钱"）必须留在 ask_info 回答问题，
    # 否则用户的问题会被一句"已记下"吞掉（探索性测试实测发现）。
    if intent in ("chitchat", "ask_info") and _has_trip_constraints(extracted):
        _hard = any(extracted.get(k) for k in (
            "origins", "target_city_name", "target_city_code",
            "party_size", "budget_band",
        ))
        if _hard or not _looks_like_question(user_message):
            intent = "provide_constraints"
            if "clarify" not in acts:
                acts.append("clarify")
    action = ROUTE_TABLE.get(intent, ("invoke", "默认"))[0]
    reply = ""
    constraints_patch: dict | None = None
    booking: dict | None = None
    pending_clarify: list[dict] = []

    if intent in ("provide_constraints", "clarify_answer"):
        # 上下文纠偏：上一轮缺的是出发地（且目的地已定）时，用户的裸城市回答应填
        # origins，而非改目的地（LLM/裸城市兜底会把城市默认抽成 target_city_name）。
        # 带"改/换/不/去/到/别"等指令词的消息视为显式改目的地，不纠偏。
        if (
            extracted.get("target_city_name")
            and not extracted.get("origins")
            and memory_ctx.get("target_city_name")
            and not memory_ctx.get("origins")
            and not re.search(r"[改换不去到别]", user_message)
        ):
            extracted["origins"] = [extracted.pop("target_city_name")]
            extracted.pop("target_city_code", None)
        merged = {**memory_ctx, **extracted}
        constraints_patch = extracted or None
        miss = missing_slots(merged)
        pending_clarify = [{"slot": m, "q": _clarify_q(m)} for m in miss[:4]]
        if extracted:
            # changed 只算与 memory 相比真正变化的键（LLM 可能照上下文原样回显已有约束，
            # 回显不算"改"——修：第三轮仍说"目的地改杭州"）
            changed_keys = {k for k, v in extracted.items() if memory_ctx.get(k) != v}
            summary = _summarize(merged, changed=changed_keys)
            reply = ("\u5df2\u8bb0\u4e0b\uff1a" + summary + "\u3002") if summary else "\u597d\u7684\uff0c\u5df2\u8bb0\u4e0b\u3002"
            if pending_clarify:
                reply += pending_clarify[0]["q"]
            else:
                # \u6709\u51fa\u53d1\u5730\u5c31\u80fd\u5f00\u59cb\uff0c\u6839\u636e\u662f\u5426\u6709\u5174\u8da3\u7ed9\u4e0d\u540c\u63d0\u793a
                city = merged.get("target_city_name") or (merged.get("origins") or [""])[0] or "\u5f53\u5730"
                if merged.get("interests"):
                    reply += "\u6b63\u5728\u4e3a\u4f60\u63a2\u7d22\u65b9\u6848\u3002"
                else:
                    reply += f"\u6211\u6765\u5e2e\u4f60\u8c03\u7814{city}\u6709\u4ec0\u4e48\u597d\u73a9\u7684\u6d3b\u52a8\uff01"
        else:
            reply = "\u60f3\u5e2e\u4f60\u627e\u8fd9\u4e2a\u5468\u672b\u7684\u597d\u53bb\u5904\u3002" + (pending_clarify[0]["q"] if pending_clarify else "")
    elif intent == "design_itinerary":
        # 点名锚点 → 路线设计（DD-15 v1.1 增补：探索阶段的分析设计能力，
        # 不再把"帮我设计路线"当普通兴趣重跑推荐）。证据纪律：库内锚点透传原
        # evidence；匹配不到的名字以 unknown 保留；场次/接驳一律 estimated。
        reply, route_plan, c_extracted = build_route_plan(
            user_message, memory_ctx, session,
            city_code=city_code, use_llm=use_llm,
            conversation=conversation, extracted=extracted,
        )
        if route_plan is not None:
            structured = interpreted.to_public_dict()
            structured.update({
                "primary_intent": semantic_primary_intent,
                "legacy_intent": intent,
                "acts": acts,
                "constraints_patch": dict(c_extracted or {}),
                "commands": [],
            })
            return {"plan_id": plan_id, "intent": intent, "action": action, "reply": reply,
                    "constraints_patch": c_extracted or None, "booking": None,
                    "pending_clarify": [], "route_plan": route_plan,
                    "itinerary_draft": interpreted.itinerary_draft,
                    "memory_note": interpreted.memory_note,
                    "acts": acts, "commands": [], "turn_decision": structured}
    elif intent == "refine_field":
        constraints_patch = extracted or None
        if "interests" in extracted and not extracted["interests"]:
            reply = "好的，已移除当前活动类型偏好，将按其他活动重新探索。"
        else:
            reply = (
                "好的，已更新：" + _summarize(extracted) + "。"
            ) if extracted else "想调整哪一项？比如「目的地改成杭州」。"
    elif intent == "deep_research":
        # 对标 Researchify：以现有结果为 baseline，携带新增偏好进入研究回环。
        feedback_text = user_message
        constraints_patch = {**extracted, "__research_feedback": feedback_text}
        action = "invoke"
        if extracted.get("soft_preferences"):
            reply = "好的，我会按你新增的偏好继续深度研究并重新排序…"
        else:
            reply = "好的，我再帮你搜搜其他选项…"
    elif intent == "confirm_booking":
        from ..domain.backfill import run_extract
        draft = run_extract("manual", "text", user_message)
        if draft.get("extracted"):
            # 抽取只是初稿。即使用户说“买好了”，字段仍需由前端/用户逐项确认后
            # 才能进入 confirmed_by_user 时间线。
            booking = {
                **draft,
                "confirmed": False,
                "ready_for_resume": False,
            }
            reply = f"已识别你的{draft['kind']}：" + _summarize_booking(draft["extracted"]) + "，确认后汇入计划。"
        else:
            reply = "没识别到车次/航班/酒店信息，可以粘贴订单文本（如 G7502 上海虹桥→杭州东 8:00）。"
    elif intent == "ask_info":
        reply = model_reply or _answer_from_db(
            user_message,
            session,
            city_code,
            use_llm=use_llm,
        )
    elif intent == "weather":
        reply = (
            "收到～天气可能有变化。当前还没有擅自改动行程；"
            "可以按“室内优先”重新规划，并保留天气来源与调整原因。"
        )
    else:  # chitchat
        reply = model_reply or _GREETING

    # The interpreter sees the durable conversation, current constraints and
    # research workspace.  Its grounded assistant reply is authoritative for
    # conversational wording; deterministic branches remain outage fallbacks.
    if model_reply:
        reply = model_reply
    if pending_clarify and not model_reply:
        question = str(pending_clarify[0].get("q") or "").strip()
        if question and question not in reply:
            reply = f"{reply}\n{question}" if reply else question

    # 混合 turn（例如“改杭州，顺便查票价”）即使主意图是问答，也不能丢掉约束更新。
    if constraints_patch is None and extracted and "update_constraints" in acts:
        constraints_patch = extracted

    # 一个 turn 可以同时更新约束并回答问题，不能因 primary_intent 只保留其中一个。
    if "answer_info" in acts and intent != "ask_info" and not model_reply:
        answer = _answer_from_db(user_message, session, city_code, use_llm=use_llm)
        reply = f"{reply}\n{answer}" if reply else answer

    commands = []
    if constraints_patch:
        public_patch = {
            key: value for key, value in constraints_patch.items()
            if key != "__research_feedback"
        }
        if public_patch:
            commands.append({"type": "update_constraints", "payload": {"patch": public_patch}})
    if "research_more" in acts:
        commands.append({
            "type": "research_more",
            "payload": {"feedback": user_message, "latest_results": latest_results or {}},
        })
    if "recompose_plan" in acts:
        commands.append({
            "type": "recompose_plan",
            "payload": {
                "instruction": user_message,
                "itinerary_draft": interpreted.itinerary_draft,
            },
        })
    if "answer_info" in acts:
        commands.append({"type": "answer", "payload": {"question": user_message}})
    if booking:
        commands.append({"type": "submit_booking_draft", "payload": {"booking": booking}})
    if "weather_replan" in acts:
        commands.append({
            "type": "request_weather_replan",
            "payload": {"reason": user_message, "from_node": "weather"},
        })
    if pending_clarify:
        commands.append({
            "type": "ask_clarification",
            "payload": {"pending": pending_clarify},
        })

    structured = interpreted.to_public_dict()
    structured.update({
        "primary_intent": semantic_primary_intent,
        "legacy_intent": intent,
        "acts": acts,
        "constraints_patch": {
            key: value for key, value in (constraints_patch or {}).items()
            if key != "__research_feedback"
        },
        "commands": commands,
    })
    return {
        "plan_id": plan_id,
        "intent": intent,
        "action": action,
        "reply": reply,
        "constraints_patch": constraints_patch,
        "booking": booking,
        "pending_clarify": pending_clarify,
        "itinerary_draft": interpreted.itinerary_draft,
        "memory_note": interpreted.memory_note,
        "acts": acts,
        "commands": commands,
        "turn_decision": structured,
    }


def _clarify_q(slot: str) -> str:
    return {"origins": "你们从哪里出发？", "weekend": "计划哪个周末出行？",
            "interests": "想去玩什么？（展览/演出/市集…）"}.get(slot, f"请补充 {slot}")
