"""DD-13 提醒通道：NotificationProvider 抽象 + Stub（无 key 时 sent=skipped，不报错）。

配齐 WTG_VAPID_* / WTG_RESEND_API_KEY 时自动切真 provider（v0.1 先 stub，可测）。
ICS 走 /calendar.ics 订阅，恒可生成 → 恒 sent。
"""
from __future__ import annotations

import logging

from ..config import get_settings

log = logging.getLogger(__name__)


class NotificationProvider:
    name = "base"

    def send(self, reminder: dict) -> str:  # sent | skipped | failed
        raise NotImplementedError


class WebPushProvider(NotificationProvider):
    name = "web_push"

    def __init__(self) -> None:
        self._key = get_settings().vapid_public_key

    def send(self, reminder: dict) -> str:
        if not self._key:
            return "skipped"  # 无 VAPID key → no-op
        # 真实 Web Push 需 VAPID + 订阅端点（v0.1 stub；配 key 后接 web-push 库）
        return "skipped"


class EmailProvider(NotificationProvider):
    name = "email"

    def __init__(self) -> None:
        self._key = get_settings().resend_api_key

    def send(self, reminder: dict) -> str:
        return "skipped" if not self._key else "skipped"  # v0.1 stub


class ICSProvider(NotificationProvider):
    name = "ics"

    def send(self, reminder: dict) -> str:
        return "sent"  # ICS 走订阅端点恒可生成


_PROVIDERS = {"web_push": WebPushProvider, "email": EmailProvider, "ics": ICSProvider}


def get_channel(name: str) -> NotificationProvider:
    return _PROVIDERS.get(name, ICSProvider)()


def dispatch(reminder: dict) -> str:
    """派发一条提醒；无 key → skipped（不报错）。"""
    try:
        return get_channel(reminder.get("channel")).send(reminder)
    except Exception as e:  # 派发失败不阻塞
        log.warning("提醒派发失败: %s", e)
        return "failed"
