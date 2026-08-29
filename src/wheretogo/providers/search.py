"""Web 搜索 Provider（DD-04 §5 / §13 v2）。

ops: web_search / web_search_deep（仅用于找官方入口；事实回官方核实，DD-03 闸一）。
real：按 WTG_SEARCH_PROVIDER 选 bocha(默认)/tavily/exa/serper（BYO WTG_SEARCH_API_KEY，多家互备）。
fallback：无 key → 返回空结果 + degraded（research 层以 source_registry 官方源清单兜底）。
"""
from __future__ import annotations

import httpx

from ..config import get_settings
from ._net import post_json
from .base import Req, Result

_DEEP_OPS = {"web_search_deep"}


class SearchProvider:
    name = "search"

    def __init__(self) -> None:
        s = get_settings()
        self._provider = (s.search_provider or "bocha").lower()
        self._key = s.search_api_key

    def call(self, req: Req) -> Result:
        if not self._key:
            return Result(ok=True, data={"results": [], "degraded_no_key": True,
                                         "deep": req.op in _DEEP_OPS},
                          source_type="search", degraded=True)
        query = req.params.get("query", "")
        count = req.params.get("count", 8)
        try:
            results = self._dispatch(query, count)
            return Result(ok=True, data={"results": results, "deep": req.op in _DEEP_OPS},
                          source_type="search")
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            detail = (exc.response.text or str(exc))[:1000]
            return Result(
                ok=False,
                data=None,
                source_type="search",
                error={
                    "type": "http_error",
                    "provider": self._provider,
                    "status_code": status_code,
                    "detail": detail,
                    "retryable": status_code not in {401, 403, 432, 433},
                },
            )
        except Exception as exc:
            return Result(
                ok=False,
                data=None,
                source_type="search",
                error={
                    "type": type(exc).__name__,
                    "provider": self._provider,
                    "detail": str(exc)[:1000],
                    "retryable": True,
                },
            )

    def _dispatch(self, query: str, count: int) -> list[dict]:
        if self._provider == "tavily":
            # advanced + raw_content：Tavily 服务端抓取并返回页面正文，绕开 JS 渲染/死链/聚合站格式问题
            data = post_json("https://api.tavily.com/search",
                             body={"api_key": self._key, "query": query, "max_results": count,
                                   "search_depth": "advanced", "include_raw_content": True,
                                   "include_answer": True, "days": 60})
            return [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content"),
                     # 把查询相关摘要放在正文前，避免长页面前 8K 导航噪声淹没命中片段。
                     "content": "\n\n".join(filter(None, [
                         f"搜索命中摘要：{r.get('content')}" if r.get("content") else None,
                         r.get("raw_content"),
                     ]))}
                    for r in (data.get("results") or [])]
        if self._provider == "serper":
            data = post_json("https://google.serper.dev/search",
                             headers={"X-API-KEY": self._key, "Content-Type": "application/json"},
                             body={"q": query, "num": count})
            return [{"title": r.get("title"), "url": r.get("link"), "snippet": r.get("snippet"),
                     "content": None}
                    for r in (data.get("organic") or [])]
        if self._provider == "exa":
            data = post_json("https://api.exa.ai/ai/contents/search",
                             headers={"x-api-key": self._key},
                             body={"query": query, "numResults": count})
            return [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("text"),
                     "content": r.get("text")}
                    for r in (data.get("results") or [])]
        # 默认 bocha（面向 AI 的中文搜索，Web Search + Semantic Reranker）
        data = post_json("https://api.bochaai.com/v1/web-search",
                         headers={"Authorization": f"Bearer {self._key}"},
                         body={"query": query, "count": count, "freshness": "oneDay"})
        web_pages = (data.get("data") or {}).get("webPages") or {}
        return [{"title": r.get("name"), "url": r.get("url"), "snippet": r.get("snippet"),
                 "content": r.get("content")}
                for r in (web_pages.get("value") or [])]


class SearchFallback:
    """无 key：空结果 + degraded；research 层以官方源清单兜底。"""

    name = "search"

    def call(self, req: Req) -> Result:
        return Result(ok=True, data={"results": [], "degraded_no_key": True,
                                     "deep": req.op in _DEEP_OPS},
                      source_type="search", degraded=True)
