"""DD-13 提醒调度：落库 build_reminders 规格 + 到期投递（P1-8 接线）。

- persist_reminders：把 compose 产出的九类提醒规格写入 reminders 表（fire_at 为空的跳过；
  重复 compose 前先清本 plan 未发送的旧调度，避免重复）。
- dispatch_due_reminders：轮询到期 scheduled 提醒 → notify.channels.dispatch 投递 → 回写状态。
  同步任务体（Celery eager 语义），无 broker 可单测；ICS 恒 sent，无 key 通道 skipped（不报错）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..db.session import SessionLocal
from ..enums import ReminderChannel, ReminderType
from ..models import Reminder
from .channels import dispatch


def _to_dt(v) -> datetime | None:
    if v is None:
        return None
    return v if isinstance(v, datetime) else datetime.fromisoformat(v)


def persist_reminders(plan_id: str | int, reminders: list[dict], session: Session | None = None) -> int:
    """落库提醒规格；fire_at 为空的跳过。返回落库条数。重复 compose 前清本 plan 的 scheduled。"""
    if not (str(plan_id) or "").isdigit():
        return 0
    own = session is None
    s = session or SessionLocal()
    try:
        s.query(Reminder).filter_by(plan_id=int(plan_id), status="scheduled").delete()
        n = 0
        for r in reminders or []:
            fire = _to_dt(r.get("fire_at"))
            if fire is None:  # fire_at 缺省 → 调度时跳过（DD-13）
                continue
            try:
                rtype = ReminderType(r.get("type"))
                chan = ReminderChannel(r.get("channel"))
            except ValueError:
                continue
            s.add(Reminder(plan_id=int(plan_id), type=rtype, channel=chan,
                           fire_at=fire, payload=r.get("payload") or {}, status="scheduled"))
            n += 1
        if own:
            s.commit()
        return n
    except Exception:
        if own:
            s.rollback()
        raise
    finally:
        if own:
            s.close()


def dispatch_due_reminders(now: datetime | None = None, session: Session | None = None) -> dict:
    """到期投递：scheduled 且 fire_at<=now → dispatch → 回写 status/sent_at。返回计数。"""
    own = session is None
    s = session or SessionLocal()
    now = now or datetime.now(timezone.utc)
    sent = skipped = failed = 0
    try:
        rows = s.query(Reminder).filter(Reminder.status == "scheduled", Reminder.fire_at <= now).all()
        for r in rows:
            ch = r.channel.value if hasattr(r.channel, "value") else r.channel
            rtype = r.type.value if hasattr(r.type, "value") else r.type
            status = dispatch({"channel": ch, "type": rtype, "payload": r.payload})
            r.status = status
            if status == "sent":
                r.sent_at = datetime.now(timezone.utc)
                sent += 1
            elif status == "skipped":
                skipped += 1
            else:
                failed += 1
        if own:
            s.commit()
        return {"due": len(rows), "sent": sent, "skipped": skipped, "failed": failed}
    finally:
        if own:
            s.close()
