"""地理确定性工具（DD-04 各 Provider 的规则兜底共用）。

本文件为 canonical 实现；retrieval/service.py 的 `_haversine_m` 后续切换到此处以消除重复。
"""
from __future__ import annotations

import math

# 直线估算的等效时速（km/h），覆盖门到门接驳主场景（DD-09 §5.1 粗估档）
_SPEED_KMH = {"walk": 5.0, "transit": 28.0, "drive": 40.0, "taxi": 45.0, "rail": 0.0, "air": 0.0}


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """两点 (lat, lng) 间球面直线距离（米）。"""
    lat1, lng1 = a
    lat2, lng2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2.0 * 6371000.0 * math.asin(math.sqrt(min(1.0, h)))


def estimate_minutes(distance_m: float, mode: str = "transit") -> int | None:
    """按模式时速把直线距离折算为接驳分钟（整数；标 estimated）。"""
    speed = _SPEED_KMH.get(mode, _SPEED_KMH["transit"])
    if speed <= 0 or distance_m is None:
        return None
    return max(1, int(distance_m / 1000.0 / speed * 60.0))
