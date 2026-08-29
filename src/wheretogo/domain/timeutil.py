"""时间公共工具（业务时区 Asia/Shanghai 的周末窗口计算）。

从 seeds/activities_dev 挪出：生产代码（domain/copilot/orchestration）不应依赖开发种子模块。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..config import SHANGHAI_TZ


def upcoming_weekend(now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(SHANGHAI_TZ)  # 业务时区：活动时间按 Asia/Shanghai 录入
    days_to_sat = (5 - now.weekday()) % 7  # 周六=5
    sat = (now + timedelta(days=days_to_sat)).replace(hour=0, minute=0, second=0, microsecond=0)
    return sat, sat + timedelta(days=2)
