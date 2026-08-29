"""HTTP 工具：各 real Provider 共用的 JSON GET/POST（httpx 同步）。"""
from __future__ import annotations

from typing import Any

import httpx


def get_json(url: str, *, headers: dict | None = None, params: dict | None = None,
             timeout: float = 15.0) -> Any:
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()


def post_json(url: str, *, headers: dict | None = None, body: Any | None = None,
              timeout: float = 30.0) -> Any:
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        return resp.json()
