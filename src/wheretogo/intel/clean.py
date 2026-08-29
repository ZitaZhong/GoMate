"""正文清洗（DD-06 §5.3）：HTML → 纯文本/Markdown。

v0.1 用 BeautifulSoup 抽取正文（去 script/style/nav 等），折叠空白；保留链接文本与少量结构。
生产可换 Readability/Jina Reader（DD-04 可封装 op="reader"），接口不变。
"""
from __future__ import annotations

from bs4 import BeautifulSoup

_DROP_TAGS = ("script", "style", "nav", "footer", "header", "noscript", "iframe", "svg")


def clean_to_markdown(html: str, base_url: str | None = None) -> str:
    """HTML → 清洗后纯文本（供 LLM 抽取）。base_url 当前仅做占位（未来相对链接归一）。"""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_DROP_TAGS):
        tag.decompose()
    # 优先取 article/main，否则整页
    root = soup.find("article") or soup.find("main") or soup
    text = root.get_text("\n", strip=True)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)
