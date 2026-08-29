"""熔断器（DD-04 §4.2，滑动窗口失败率）。

closed → 连续/窗口失败达阈值 → open（直接走降级，不打主调）→ 冷却后半开探测 →
成功达标 closed / 失败再 open。进程内实现。
"""
from __future__ import annotations

import threading
import time
from collections import deque

_HALF_OPEN_COOLDOWN = 30.0  # 秒；open 后多久进入半开探测


class CircuitBreaker:
    def __init__(self, fail_threshold: int = 5, recovery_successes: int = 2) -> None:
        self.fail_threshold = fail_threshold
        self.recovery_successes = recovery_successes
        self._state: dict[str, str] = {}  # provider -> closed|open|half_open
        self._failures: dict[str, deque[float]] = {}
        self._opened_at: dict[str, float] = {}
        self._half_successes: dict[str, int] = {}
        self._lock = threading.Lock()

    def _state_of(self, provider: str) -> str:
        return self._state.get(provider, "closed")

    def is_open(self, provider: str) -> bool:
        with self._lock:
            state = self._state_of(provider)
            if state == "open":
                if time.monotonic() - self._opened_at.get(provider, 0) > _HALF_OPEN_COOLDOWN:
                    self._state[provider] = "half_open"
                    self._half_successes[provider] = 0
                    return False  # 放行一次探测
                return True
            return False

    def record_success(self, provider: str) -> None:
        with self._lock:
            self._failures.pop(provider, None)
            if self._state_of(provider) == "half_open":
                self._half_successes[provider] = self._half_successes.get(provider, 0) + 1
                if self._half_successes[provider] >= self.recovery_successes:
                    self._state[provider] = "closed"
                    self._half_successes.pop(provider, None)

    def record_failure(self, provider: str) -> None:
        with self._lock:
            state = self._state_of(provider)
            if state == "half_open":
                self._state[provider] = "open"
                self._opened_at[provider] = time.monotonic()
                return
            dq = self._failures.setdefault(provider, deque(maxlen=self.fail_threshold))
            dq.append(time.monotonic())
            if len(dq) >= self.fail_threshold:
                self._state[provider] = "open"
                self._opened_at[provider] = time.monotonic()

    def state(self, provider: str) -> str:
        with self._lock:
            return self._state_of(provider)
