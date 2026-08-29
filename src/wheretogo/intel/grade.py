"""定级（DD-06 §5.7）：逐字段过 DD-03 闸一/闸二，整体取最保守态（决定检索可见性）。"""
from __future__ import annotations

from datetime import datetime

from ..enums import VerificationStatus
from ..orchestration.guard import enforce_provenance, most_conservative_status, validate_fact
from ..schemas.evidence import Evidence


def grade_activity(n, src, fetched_at: datetime | None = None) -> dict:
    """对活动的对外事实字段逐一定级，产出落库用 evidence JSONB + 整体 verification_status。

    search/community 来源未核实 → unknown（保留入口信息，待 Phase 3 核实升级）；
    官方源 + URL → official_source_confirmed；缺 URL 经闸二降为 unknown。永不自造 confirmed。
    """
    st = src.source_type
    url = n.source_url
    facts = {
        "activity.start_at": validate_fact(
            enforce_provenance("activity.start_at", n.start_at, st, url, fetched_at)),
        "activity.price_text": validate_fact(
            enforce_provenance("activity.price_text", n.price_text, st, url, fetched_at)),
        "activity.booking_url": validate_fact(
            enforce_provenance("activity.booking_url", n.booking_url, st, url, fetched_at)),
    }
    # 整体态：只对“有值”字段取最保守（None/空串的可选字段如 booking_url 不应拖累整体到 unknown）
    present = [f.evidence.verification_status for f in facts.values()
               if f.value not in (None, "")]
    overall = most_conservative_status(present) if present else VerificationStatus.unknown
    base = facts["activity.start_at"].evidence
    overall_ev = Evidence(
        source_type=base.source_type, source_url=base.source_url, fetched_at=fetched_at,
        verification_status=overall, confidence=base.confidence, note=base.note,
    )
    return {
        "evidence": overall_ev.to_jsonb(),
        "verification_status": overall.value if isinstance(overall, VerificationStatus) else overall,
        "field_facts": {k: {"value": f.value, "evidence": f.evidence.to_jsonb()} for k, f in facts.items()},
    }
