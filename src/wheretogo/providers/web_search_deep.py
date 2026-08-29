"""web 深搜 Provider 别名（DD-04 §13 v2 / DD-17）。

v0.1 复用 SearchProvider 处理 op=`web_search_deep`；"深搜"的并发/反思/收敛编排由
research/service.py（DD-17）承担，本 Provider 只提供"找官方入口"的搜索原子能力。
"""
from __future__ import annotations

from .search import SearchFallback, SearchProvider

__all__ = ["SearchProvider", "SearchFallback"]
