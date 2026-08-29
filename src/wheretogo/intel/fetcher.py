"""抓取 Fetcher（DD-06 §5.2）：httpx + robots + ETag + 双层限速 + 真实 UA。

合规红线：robots 强制（取不到默认不抓）、双层限速、真实 UA、只公开页。
Playwright 为可选懒加载（JS 站）；缺省 httpx。`fetch_page(..., allow_fetch=fn)` 可注入 HTTP
函数供单测（不触网）。
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urlparse

import httpx

UA = "WhereToGoBot/0.1 (+https://wheretogo.example/bot)"
_DOMAIN_INTERVAL = 1.0  # 单域名最小间隔（秒）→ 单域 1 QPS
_GLOBAL_QPS = 5.0  # 全局上限

_robots_cache: dict[str, robotparser.RobotFileParser | None] = {}
_last_fetch: dict[str, float] = {}
_last_global = [0.0]


@dataclass
class FetchResult:
    url: str
    http_status: int | None
    html: str | None
    etag: str | None
    content_hash: str | None
    from_cache: bool = False  # 304 Not Modified
    robots_allowed: bool = True
    error: str | None = None


def _robots_ok(url: str) -> bool:
    parts = urlparse(url)
    host = f"{parts.scheme}://{parts.netloc}"
    if host not in _robots_cache:
        rp = robotparser.RobotFileParser()
        rp.set_url(f"{host}/robots.txt")
        try:
            rp.read()
            _robots_cache[host] = rp
        except Exception:
            _robots_cache[host] = None  # 取不到 robots → 默认不抓（合规）
    rp = _robots_cache[host]
    return bool(rp) and rp.can_fetch(UA, url)


def _throttle(netloc: str) -> None:
    now = time.monotonic()
    wait_g = max(0.0, _last_global[0] + 1.0 / _GLOBAL_QPS - now)
    wait_d = 0.0
    prev = _last_fetch.get(netloc)
    if prev is not None:
        wait_d = max(0.0, prev + _DOMAIN_INTERVAL - now)
    time.sleep(max(wait_g, wait_d))
    t = time.monotonic()
    _last_global[0] = t
    _last_fetch[netloc] = t


def _http_get(url: str, etag: str | None = None, timeout: float = 15.0):
    headers = {"User-Agent": UA}
    if etag:
        headers["If-None-Match"] = etag
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        return client.get(url, headers=headers)


def _playwright_get(url: str, timeout: float = 20.0):
    """Playwright 渲染 JS 页（httpx 正文过薄的兜底，DD-06 §5.2 可选懒加载）。

    返回 (status, rendered_html)；失败抛异常（调用方捕获后回退 httpx 结果）。
    真实浏览器加载 + networkidle 等待，能拿到 SPA 动态渲染的活动正文。
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=UA)
            resp = page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            try:
                page.wait_for_load_state("networkidle", timeout=8000)  # 等 JS 渲染稳态
            except Exception:
                pass  # 超时不阻塞，用已渲染内容
            html = page.content()
            status = resp.status if resp else 200
        finally:
            browser.close()
    return status, html


def fetch_page(src, etag: str | None = None, allow_fetch=None) -> FetchResult:
    """抓单源首页。src 需有 entry_url/robots_ok；allow_fetch 可注入（测试）。"""
    url = src.entry_url
    if not getattr(src, "robots_ok", True) or not _robots_ok(url):
        return FetchResult(url=url, http_status=None, html=None, etag=None,
                           content_hash=None, robots_allowed=False)
    fetch = allow_fetch or _http_get
    try:
        _throttle(urlparse(url).netloc)
        resp = fetch(url, etag=etag)
    except Exception as e:
        return FetchResult(url=url, http_status=None, html=None, etag=None,
                           content_hash=None, error=str(e))
    if resp.status_code == 304:
        return FetchResult(url=url, http_status=304, html=None, etag=etag,
                           content_hash=None, from_cache=True)
    html = resp.text if resp.status_code == 200 else None
    chash = hashlib.sha1((html or "").encode()).hexdigest() if html else None
    new_etag = resp.headers.get("etag") or etag
    return FetchResult(url=url, http_status=resp.status_code, html=html,
                       etag=new_etag, content_hash=chash)
