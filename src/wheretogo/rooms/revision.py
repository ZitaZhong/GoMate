"""DD-18 §5 AI 自然语言修改：类型识别（LLM 主路径 + 规则兜底）与局部更新。

局部修改原则（§5.2）：只改受影响节点；保留核心活动与成员路线；重算受影响的后续
节点时间；识别失败 → 降级"换一批活动"兜底（§9）。
确认规则（§5.3）：删核心活动 / 整体延后超窗 / 预算超限 / 需重新购票 → needs_confirmation。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from ..enums import RevisionType
from ..providers import extract_json

# 规则兜底：关键词 → (修改类型, 目标节点类型)；顺序即优先级
_RULES: list[tuple[RevisionType, str | None, list[str]]] = [
    (RevisionType.full_replan, None, ["重新规划", "全部重来", "推倒重来", "重头再来", "全部换"]),
    (RevisionType.change_theme, None, ["换主题", "改主题", "换个主题", "不想看展", "换一类"]),
    (RevisionType.change_transport, None,
     ["地铁去", "开车去", "打车去", "骑车去", "坐公交", "换交通", "改交通", "不开车"]),
    (RevisionType.adjust_budget, None, ["预算", "便宜点", "太贵", "贵了", "降低花费", "人均"]),
    (RevisionType.adjust_time, None,
     ["提前", "推迟", "延后", "改到", "早点", "晚点", "点开始", "点结束"]),
    (RevisionType.remove_node, "dining", ["不吃", "取消晚餐", "取消午餐", "不用吃饭", "删掉餐厅"]),
    (RevisionType.remove_node, "activity", ["取消活动", "删掉活动", "不去了"]),
    (RevisionType.add_node, "dining", ["加个饭", "加一顿", "加个夜宵", "加个下午茶", "顺便吃"]),
    (RevisionType.add_node, None, ["加一个", "再加", "加个", "添加"]),
    (RevisionType.replace_node, "dining",
     ["换一家", "换个餐厅", "换餐厅", "别的餐厅", "换个地方吃", "不辣的", "换家店"]),
    (RevisionType.replace_node, "activity", ["换个活动", "换活动", "换一个活动", "别的活动"]),
]

_TIME_RE = re.compile(r"(提前|推迟|延后)\s*(半小时|一小时|两小时|(\d+)\s*分钟|(\d+)\s*小时)")
_CLOCK_RE = re.compile(r"改到\s*(\d{1,2})[点:：](\d{2})?")


def classify_revision(message: str) -> dict:
    """识别修改意图。返回 {revision_type, target_kind, keyword, degraded}。

    LLM 主路径（语义理解任意表达）→ 规则兜底 → 全失败降级"换一批活动"。
    """
    msg = (message or "").strip()
    if not msg:
        return _fallback()
    parsed = extract_json(
        "room_revision_classify",
        "识别用户对周末活动行程的修改意图。输出 JSON "
        "{revision_type, target_kind, keyword}。"
        "revision_type 取值：replace_node(换活动/换餐厅)/add_node(新增)/remove_node(删除)/"
        "adjust_time(调时间)/adjust_budget(调预算)/change_transport(改交通)/"
        "change_theme(换主题)/full_replan(全部重来)。"
        "target_kind 取值：activity/dining/transport/null。"
        "keyword 是用户的具体要求（如'不辣的'），无则 null",
        msg,
    )
    if isinstance(parsed, dict) and parsed.get("revision_type"):
        try:
            rtype = RevisionType(str(parsed["revision_type"]))
            return {
                "revision_type": rtype.value,
                "target_kind": parsed.get("target_kind") or _guess_target(msg, rtype),
                "keyword": parsed.get("keyword") or _extract_keyword(msg),
                "degraded": False,
            }
        except ValueError:
            pass  # LLM 给出未知类型 → 规则兜底
    for rtype, target, kws in _RULES:
        if any(k in msg for k in kws):
            return {
                "revision_type": rtype.value,
                "target_kind": target or _guess_target(msg, rtype),
                "keyword": _extract_keyword(msg),
                # 离线规则识别：命中也属降级路径，诚实标注（与全失败兜底一致）
                "degraded": True,
            }
    return _fallback()


def _fallback() -> dict:
    """识别失败 → "换一批活动"兜底（DD-18 §9）。"""
    return {"revision_type": RevisionType.replace_node.value, "target_kind": "activity",
            "keyword": None, "degraded": True}


def _guess_target(msg: str, rtype: RevisionType) -> str | None:
    if any(k in msg for k in ("餐", "吃", "饭", "菜", "辣", "火锅", "咖啡", "下午茶")):
        return "dining"
    if any(k in msg for k in ("活动", "展", "演出", "场馆")):
        return "activity"
    if rtype in (RevisionType.replace_node, RevisionType.add_node, RevisionType.remove_node):
        return "activity"
    return None


def _extract_keyword(msg: str) -> str | None:
    """抽取具体要求关键词（如"不辣的"）；简化：取"的"前的修饰短语。"""
    m = re.search(r"([不没][\u4e00-\u9fa5]{1,6}的|[\u4e00-\u9fa5]{2,6}一点)", msg)
    return m.group(1) if m else None


# ============================ 局部更新 ============================
def apply_revision(
    itinerary: dict,
    decision: dict,
    message: str,
    replacement: dict | None = None,
    common_window_end: str | None = None,
    min_member_budget: int | None = None,
) -> tuple[dict, list[str], list[str]]:
    """对行程 payload 做局部更新。返回 (新 payload, 改动节点 titles, 需确认原因列表)。

    只深拷贝并修改受影响节点；其余节点保持引用语义不变（§5.2）。
    replacement：上层预取的替换候选（餐厅/活动）；无则以"待确认"占位（estimated）。
    """
    import copy

    payload = copy.deepcopy(itinerary)
    nodes: list[dict] = payload.setdefault("nodes", [])
    rtype = RevisionType(decision["revision_type"])
    target_kind = decision.get("target_kind")
    keyword = decision.get("keyword")
    changed: list[str] = []
    confirms: list[str] = []

    if rtype == RevisionType.full_replan:
        confirms.append("将重新规划全部行程，需要确认")
        payload["pending_full_replan"] = True
        return payload, changed, confirms

    if rtype == RevisionType.change_theme:
        confirms.append("修改整体主题会替换核心活动，需要确认")
        payload["pending_theme_change"] = message
        return payload, changed, confirms

    if rtype == RevisionType.replace_node:
        idx = _find_node(nodes, target_kind)
        if idx is None:
            return payload, changed, confirms
        old = nodes[idx]
        if old.get("type") == "activity" and old.get("booking_url"):
            confirms.append("原活动可能已购票/预约，替换需重新购票")
        new_node = dict(old)
        if replacement:
            new_node.update({k: v for k, v in replacement.items() if v is not None})
            new_node["evidence"] = replacement.get("evidence") or {
                "verification_status": "public_source_observed"}
        else:
            req = keyword or "按要求更换"
            new_node["title"] = f"{req}的{'餐厅' if target_kind == 'dining' else '活动'}（待确认）"
            new_node["evidence"] = {"verification_status": "estimated",
                                    "note": "无实时候选，建议到点评/官方确认"}
        new_node["revision_note"] = message
        nodes[idx] = new_node
        changed.append(str(old.get("title") or target_kind))
        return payload, changed, confirms

    if rtype == RevisionType.remove_node:
        idx = _find_node(nodes, target_kind)
        if idx is None:
            return payload, changed, confirms
        old = nodes[idx]
        if old.get("type") == "activity":
            confirms.append("删除核心活动需要确认")
            payload["pending_remove"] = old.get("title")
            return payload, changed, confirms
        nodes.pop(idx)
        changed.append(str(old.get("title") or target_kind))
        return payload, changed, confirms

    if rtype == RevisionType.add_node:
        node = {
            "type": target_kind or "activity",
            "title": (replacement or {}).get("title")
            or f"{keyword or '新增安排'}（待确认）",
            "evidence": (replacement or {}).get("evidence")
            or {"verification_status": "estimated"},
            "revision_note": message,
        }
        nodes.append(node)
        changed.append(str(node["title"]))
        return payload, changed, confirms

    if rtype == RevisionType.adjust_time:
        delta = _parse_time_adjustment(message, nodes)
        if delta is None:
            # 没解析出怎么调 → 显式说明，绝不静默默认平移（修："改到15:00"被按 +30 分钟改错）
            payload["time_adjust_unparsed"] = True
            confirms.append(
                "没理解要调整到什么时间，未改动；请说具体点（如「整体晚一小时」「改到15:00」）"
            )
            return payload, changed, confirms
        for n in nodes:
            for key in ("start", "end"):
                if n.get(key):
                    try:
                        n[key] = (datetime.fromisoformat(n[key]) + delta).isoformat()
                    except (ValueError, TypeError):
                        pass
            changed.append(str(n.get("title") or n.get("type")))
        # 延后导致超过共同结束时间 → 需确认（§5.3）
        if common_window_end and delta.total_seconds() > 0:
            last_end = _last_end(nodes)
            if last_end and last_end.time().isoformat(timespec="minutes") > common_window_end:
                confirms.append("整体延后可能导致部分成员无法按时结束")
        payload["time_shift_minutes"] = int(delta.total_seconds() // 60)
        return payload, changed, confirms

    if rtype == RevisionType.adjust_budget:
        payload["budget_note"] = message
        # 预算明显超限需确认（§5.3）——上调场景
        if min_member_budget and any(k in message for k in ("提高", "升", "加预算", "贵")):
            confirms.append("预算调整可能超出部分成员限制")
        changed.append("预算约束")
        return payload, changed, confirms

    if rtype == RevisionType.change_transport:
        for route in payload.get("member_routes") or []:
            route["transport_note"] = message
        payload["transport_change"] = message
        confirms.append("修改交通方式后部分成员通勤时间可能明显增加")
        changed.append("交通方式")
        return payload, changed, confirms

    return payload, changed, confirms


def _find_node(nodes: list[dict], kind: str | None) -> int | None:
    for i, n in enumerate(nodes):
        if n.get("type") == (kind or "activity"):
            return i
    return None


def _parse_time_adjustment(msg: str, nodes: list[dict]) -> timedelta | None:
    """解析时间调整量。返回 None = 未识别（调用方必须显式说明，不得默认平移）。

    三种形态：①「提前/推迟/延后 + 时长」相对平移；②「改到15:00」绝对时刻——
    以核心活动节点 start 为锚对齐目标时刻，其余节点等幅平移；
    ③裸「提前/早点/推迟/晚点」无时长 → 方向性默认 30 分钟。
    """
    m = _TIME_RE.search(msg)
    if m:
        sign = -1 if m.group(1) == "提前" else 1
        unit = m.group(2)
        if unit == "半小时":
            minutes = 30
        elif unit == "一小时":
            minutes = 60
        elif unit == "两小时":
            minutes = 120
        elif m.group(3):
            minutes = int(m.group(3))
        else:
            minutes = int(m.group(4)) * 60
        return timedelta(minutes=sign * minutes)
    cm = _CLOCK_RE.search(msg)
    if cm:
        hour, minute = int(cm.group(1)), int(cm.group(2) or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        anchor: datetime | None = None
        for n in sorted(nodes, key=lambda x: 0 if x.get("type") == "activity" else 1):
            if n.get("start"):
                try:
                    anchor = datetime.fromisoformat(n["start"])
                    break
                except (ValueError, TypeError):
                    continue
        if anchor is None:
            return None
        target = anchor.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return target - anchor
    if "提前" in msg or "早点" in msg:
        return timedelta(minutes=-30)
    if "推迟" in msg or "延后" in msg or "晚点" in msg:
        return timedelta(minutes=30)
    return None


def _last_end(nodes: list[dict]) -> datetime | None:
    ends = []
    for n in nodes:
        if n.get("end"):
            try:
                ends.append(datetime.fromisoformat(n["end"]))
            except (ValueError, TypeError):
                pass
    return max(ends) if ends else None
