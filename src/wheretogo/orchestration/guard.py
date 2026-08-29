"""Provenance Guard（DD-03 精简落地；DD-02 §5 compose 最终闸、§12 硬 KPI）。

铁律：**未确认字段被误标为已确认 = 0**。任何一次违规即拦截出稿并计数报警。
判定：
  - source_type == llm 的字段，永远不得标为 confirmed（confirmed_by_user/official_source_confirmed）；
  - official_source_confirmed 只能来自官方/权威数据源；
  - confirmed_by_user 只能来自用户提供（回填确认）。
"""
from __future__ import annotations

from collections.abc import Iterator

from ..enums import SourceType, VerificationStatus
from ..schemas.evidence import CONFIRMED_STATUSES, Evidence, Fact

#: 可支撑“官方确认”的来源
_OFFICIAL_SOURCES = {
    SourceType.official_venue.value,
    SourceType.culture_bureau.value,
    SourceType.open_dataset.value,
    SourceType.amap.value,
    SourceType.qweather.value,
    SourceType.variflight.value,
}


class ProvenanceViolation(Exception):
    """出稿前发现“未确认误标为已确认”。"""

    def __init__(self, violations: list[dict]):
        self.violations = violations
        super().__init__(f"Provenance Guard 拦截：{len(violations)} 处未确认字段被误标为已确认")


def iter_evidence(obj: object) -> Iterator[dict]:
    """递归遍历，产出所有内嵌的 evidence dict。"""
    if isinstance(obj, dict):
        ev = obj.get("evidence")
        if isinstance(ev, dict):
            yield ev
        for v in obj.values():
            yield from iter_evidence(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_evidence(item)


def verify_evidence(ev: dict) -> str | None:
    """校验单条 evidence；返回违规原因或 None。"""
    vs = ev.get("verification_status")
    src = ev.get("source_type")
    confirmed = {s.value for s in CONFIRMED_STATUSES}
    if vs not in confirmed:
        return None  # 非“已确认”展示，交由前端六态区分，无需 Guard 拦截
    if src == SourceType.llm.value:
        return f"LLM 来源不得标为已确认（vs={vs}）"
    if vs == VerificationStatus.official_source_confirmed.value and src not in _OFFICIAL_SOURCES:
        return f"官方确认态来源不合法：source_type={src}"
    if vs == VerificationStatus.confirmed_by_user.value and src != SourceType.user_provided.value:
        return f"用户确认态来源应为 user_provided，实际={src}"
    return None


def run_guard(obj: object) -> list[dict]:
    """扫描对象树，返回违规列表（空则通过）。"""
    violations: list[dict] = []
    for ev in iter_evidence(obj):
        reason = verify_evidence(ev)
        if reason:
            violations.append({"evidence": ev, "reason": reason})
    return violations


def assert_guard(obj: object) -> None:
    """出稿前断言：有任何违规立即抛出（compose 节点调用）。"""
    violations = run_guard(obj)
    if violations:
        raise ProvenanceViolation(violations)


# ============================ DD-03 §6 三闸（字段级定级）============================
# 字段 → 可“官方确认”的来源集（不在集内 → 至多 public_source_observed/estimated）
_FIELD_OFFICIAL_SOURCES: dict[str, frozenset[SourceType]] = {
    "activity.start_at": frozenset(
        {SourceType.official_venue, SourceType.culture_bureau, SourceType.open_dataset,
         SourceType.editorial}
    ),
    "activity.price_text": frozenset(
        {SourceType.official_venue, SourceType.culture_bureau, SourceType.open_dataset,
         SourceType.editorial}
    ),
    "activity.booking_url": frozenset(
        {SourceType.official_venue, SourceType.culture_bureau, SourceType.open_dataset}
    ),
    "route.minutes": frozenset({SourceType.amap}),
    "route.distance_m": frozenset({SourceType.amap}),
    "weather.precip": frozenset({SourceType.qweather}),
    "weather.temp": frozenset({SourceType.qweather}),
    "flight.schedule": frozenset({SourceType.variflight}),
}
_DEFAULT_OFFICIAL_SOURCES = frozenset(
    {SourceType.official_venue, SourceType.culture_bureau, SourceType.open_dataset,
     SourceType.editorial}
)
# 六态保守序（rank 小 = 更保守/不可信）
_STATUS_ORDER = [
    VerificationStatus.expired, VerificationStatus.unknown, VerificationStatus.estimated,
    VerificationStatus.public_source_observed, VerificationStatus.official_source_confirmed,
    VerificationStatus.confirmed_by_user,
]


def _to_source_type(st: SourceType | str) -> SourceType:
    if isinstance(st, SourceType):
        return st
    return SourceType(st)


def status_rank(s: VerificationStatus | str) -> int:
    sv = s.value if isinstance(s, VerificationStatus) else s
    try:
        return _STATUS_ORDER.index(VerificationStatus(sv))
    except ValueError:
        return 0  # 未知态视作最保守


def most_conservative_status(statuses) -> VerificationStatus:
    """取最保守（最不可信）的一档——决定检索可见性（DD-06 §5.7 整体态）。"""
    return min(statuses, key=status_rank)


def enforce_provenance(field: str, value, source_type: SourceType | str,
                       source_url: str | None = None, fetched_at=None) -> Fact:
    """闸一：字段级白名单定级（DD-03 §6）。

    来源不足以支撑该字段“官方确认” → 降级 estimated/public/unknown；永不自造 confirmed。
    """
    st = _to_source_type(source_type)
    if value is None or value == "":
        return Fact(value=value, evidence=Evidence.unknown(st, note=f"{field} 缺值"))
    if st == SourceType.llm:
        return Fact(value=value, evidence=Evidence.estimated(st, note=f"{field} LLM 抽取"))
    allowed = _FIELD_OFFICIAL_SOURCES.get(field, _DEFAULT_OFFICIAL_SOURCES)
    if st in allowed:
        if source_url:
            return Fact(
                value=value,
                evidence=Evidence(
                    source_type=st, source_url=source_url, fetched_at=fetched_at,
                    verification_status=VerificationStatus.official_source_confirmed, confidence=0.85,
                ),
            )
        return Fact(value=value, evidence=Evidence.estimated(st, note=f"{field} 缺官方 URL"))
    # search/community 仅找入口、未经第二来源核实 → unknown（DD-03 §4 map_status）；
    # 保留 url/confidence/note，核实通过后由 supervisor Phase 3 升级（只升不降）
    if st in (SourceType.search, SourceType.community):
        return Fact(
            value=value,
            evidence=Evidence(
                source_type=st, source_url=source_url, fetched_at=fetched_at,
                verification_status=VerificationStatus.unknown, confidence=0.4,
                note=f"{field} 来自 {st.value}，仅作入口未核实",
            ),
        )
    # user_provided：有 url→public_source_observed，否则 estimated
    if source_url and st == SourceType.user_provided:
        return Fact(
            value=value,
            evidence=Evidence(
                source_type=st, source_url=source_url, fetched_at=fetched_at,
                verification_status=VerificationStatus.public_source_observed, confidence=0.4,
            ),
        )
    return Fact(value=value, evidence=Evidence.estimated(st, note=f"{field} 来源 {st.value} 不足"))


def validate_fact(fact: Fact) -> Fact:
    """闸二：声称 official/confirmed 但缺 source_url → 降级 unknown（DD-03 §6）。"""
    ev = fact.evidence
    if ev.verification_status in CONFIRMED_STATUSES and not ev.source_url:
        ev.verification_status = VerificationStatus.unknown
        ev.confidence = 0.0
        ev.note = (ev.note + "；" if ev.note else "") + "闸二：缺 source_url 降级 unknown"
    return fact


def assert_no_fabricated_transport(bundle: object) -> None:
    """闸三：扫描 bundle，交通字段（train.*/flight.*/transport price|availability|schedule）
    若 evidence.source_type == llm → 抛 ProvenanceViolation（CI 红线，DD-03 §6/§闸三）。

    bundle 为渲染就绪结构：每个事实字段可携带 evidence dict；本函数递归按"路径"匹配交通字段。
    """
    transport_tokens = ("train", "flight", "transport", "rail", "air", "presale")
    violations: list[dict] = []

    def scan(obj, path: str) -> None:
        if isinstance(obj, dict):
            ev = obj.get("evidence")
            if isinstance(ev, dict) and ev.get("source_type") == SourceType.llm.value:
                low = path.lower()
                if any(tok in low for tok in transport_tokens) or "price" in low or "availability" in low:
                    violations.append({"path": path, "evidence": ev, "reason": "交通字段不得来自 LLM"})
            for k, v in obj.items():
                scan(v, f"{path}.{k}" if path else str(k))
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                scan(item, f"{path}[{i}]")

    scan(bundle, "")
    if violations:
        raise ProvenanceViolation(violations)
