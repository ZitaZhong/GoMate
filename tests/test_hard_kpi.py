"""三条硬 KPI（CI 门禁，恒为 0）+ 越权写扫描。

① 未确认误展为已确认 = 0（DD-03/13 run_final_gate）
② 硬冲突率 = 0（DD-12 solve_timeline 冲突感知 + validate）
③ 越权写 activities = 0（DD-01/06：activities 只由 intel/seeds 写）
另：交通字段禁编（DD-03 闸三 assert_no_fabricated_transport）
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import wheretogo
from wheretogo.domain.compose import run_final_gate
from wheretogo.domain.timeline import solve_timeline, validate_timeline
from wheretogo.orchestration.guard import ProvenanceViolation, assert_no_fabricated_transport


def _act(title, start, end):
    return {"title": title, "start_at": start, "end_at": end, "id": 1,
            "evidence": {"source_type": "official_venue", "verification_status": "official_source_confirmed"}}


# —— KPI① ——
def test_kpi1_unconfirmed_never_shown_as_confirmed():
    clean = {"activities": [{"evidence": {"source_type": "official_venue",
                "verification_status": "official_source_confirmed"}}]}
    run_final_gate(clean)  # 干净 bundle 不抛
    bad = {"activities": [{"evidence": {"source_type": "llm",
                "verification_status": "official_source_confirmed"}}]}
    with pytest.raises(ProvenanceViolation):
        run_final_gate(bad)  # LLM 来源误标已确认 → 拦截


# —— KPI② ——
def test_kpi2_hard_conflict_rate_zero():
    base = datetime(2026, 7, 25, 10, tzinfo=timezone.utc)
    acts = [
        _act("A", base.isoformat(), (base + timedelta(hours=2)).isoformat()),
        _act("B", (base + timedelta(hours=1)).isoformat(), (base + timedelta(hours=3)).isoformat()),  # 与 A 重叠
        _act("C", (base + timedelta(hours=3)).isoformat(), (base + timedelta(hours=5)).isoformat()),
    ]
    slots = solve_timeline(acts, [], [], {})
    v = validate_timeline(slots, {})
    assert "HARD_CONFLICT" not in (v["issues"] or [])
    assert v["metrics"]["hard_conflict"] is False  # 出稿版硬冲突率 = 0


# —— KPI③ + 交通禁编 ——
def test_kpi3_transport_no_llm_and_no_unauthorized_activity_writes():
    # 交通字段不得来自 LLM
    assert_no_fabricated_transport({})  # 空 bundle 过
    with pytest.raises(ProvenanceViolation):
        assert_no_fabricated_transport({"transport": {"price": {"evidence": {"source_type": "llm"}}}})
    # activities 只由 intel/seeds 写；规划流/domain/retrieval/providers 等不得写
    root = Path(wheretogo.__file__).parent
    scan_dirs = {"orchestration", "domain", "research", "copilot", "memory", "providers", "retrieval", "bff"}
    write_re = re.compile(
        r"session\.add\(\s*Activity|\.add\(\s*Activity\b|pg_insert\(Activity\)|"
        r"INSERT\s+INTO\s+activities", re.I)
    offenders = []
    for d in scan_dirs:
        for p in (root / d).rglob("*.py"):
            txt = p.read_text(encoding="utf-8")
            if write_re.search(txt):
                offenders.append(str(p.relative_to(root)))
    assert not offenders, f"规划流越权写 activities: {offenders}"
