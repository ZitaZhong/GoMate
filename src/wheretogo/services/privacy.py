"""用户数据权利：一键导出 / 一键删除（DD-01 §11.4）。

函数以 session 为首参，便于测试注入事务性会话；BFF 层用 `with get_session()` 包裹。
删除按“先删 plans（级联行程域）再删 user（级联 user_context）”执行，兑现 §11.4“级联删除”语义。
"""
from __future__ import annotations

from typing import TypedDict

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import Plan, TripBundle, User, UserContext


class PrivacyExport(TypedDict):
    user: dict | None
    context: dict | None
    plans: list[dict]


def export_user_data(session: Session, user_id: int) -> PrivacyExport:
    """导出某用户的 plans + bundles + context（JSON 就绪）。"""
    user = session.get(User, user_id)
    if user is None:
        return {"user": None, "context": None, "plans": []}

    ctx = session.get(UserContext, user_id)
    plans_out: list[dict] = []
    plans = session.scalars(select(Plan).where(Plan.organizer_user_id == user_id)).all()
    for p in plans:
        bundles = session.scalars(select(TripBundle).where(TripBundle.plan_id == p.id)).all()
        plans_out.append(
            {
                "id": p.id,
                "stage": p.stage.value if hasattr(p.stage, "value") else p.stage,
                "thread_id": p.thread_id,
                "constraints": p.constraints,
                "bundles": [{"version": b.version, "payload": b.payload} for b in bundles],
            }
        )

    return {
        "user": {"id": user.id, "anon_id": user.anon_id},
        "context": None
        if ctx is None
        else {
            "home_cities": ctx.home_cities,
            "budget_band": ctx.budget_band,
            "interests": ctx.interests,
            "dietary": ctx.dietary,
            "visited": ctx.visited,
        },
        "plans": plans_out,
    }


def delete_user_data(session: Session, user_id: int) -> dict[str, int]:
    """级联删除该用户的全部数据。返回删除计数。"""
    plan_ids = list(session.scalars(select(Plan.id).where(Plan.organizer_user_id == user_id)))
    # 先删 plans（ON DELETE CASCADE 清理 members/party/bookings/dining/routes/timeline/bundles/reminders）
    session.execute(delete(Plan).where(Plan.organizer_user_id == user_id))
    # 再删 user（ON DELETE CASCADE 清理 user_context）
    session.execute(delete(User).where(User.id == user_id))
    return {"deleted_user": user_id, "deleted_plans": len(plan_ids)}
