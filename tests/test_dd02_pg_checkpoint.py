"""DD-02 §9/§13.2：PostgresSaver 跨实例（模拟跨进程/隔日）恢复验证。

用两个独立 PlannerService（各自独立 psycopg 连接）共享同一 thread：
实例A 启动到中断 → 实例B 从 Postgres checkpoint 恢复并跑到确认版。
"""
from __future__ import annotations

import uuid

from wheretogo.orchestration import PlannerService, make_postgres_checkpointer

SH = {"query": "周末 展览", "interests": ["展览"], "target_city_code": "310000"}
BOOKING = [
    {
        "kind": "hotel",
        "extracted": {"name": "示例酒店"},
        "confirmed": True,
        "evidence": {"source_type": "user_provided", "verification_status": "confirmed_by_user", "confidence": 1.0},
    }
]


def _close(cp) -> None:
    conn = getattr(cp, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def test_postgres_checkpoint_cross_instance_resume():
    plan_id = "pgtest-" + uuid.uuid4().hex[:8]
    cp1 = make_postgres_checkpointer()
    cp2 = None
    try:
        svc1 = PlannerService(cp1)
        r1 = svc1.start(plan_id, SH)
        assert r1["interrupt"] is not None
        assert r1["interrupt"]["type"] == "await_booking"

        # 新实例 + 新连接：模拟隔日/新进程恢复
        cp2 = make_postgres_checkpointer()
        svc2 = PlannerService(cp2)
        r2 = svc2.resume(plan_id, BOOKING)
        assert r2["state"]["stage"] == "confirm"
        assert r2["state"]["bundle"]["version"] == "confirm"
        assert r2["state"]["bookings"] == BOOKING
    finally:
        _close(cp1)
        _close(cp2)
