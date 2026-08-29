"""DD-14 浏览器扩展验收：MV3 最小权限、不自动登录/购票、文件齐备。"""
from __future__ import annotations

import json
from pathlib import Path

_EXT = Path(__file__).resolve().parents[1] / "extension"


def test_extension_files_present():
    for f in ["manifest.json", "content.js", "popup.html", "popup.js", "options.html", "background.js"]:
        assert (_EXT / f).exists(), f"缺少扩展文件: {f}"


def test_manifest_mv3_minimal_permissions():
    m = json.loads((_EXT / "manifest.json").read_text(encoding="utf-8"))
    assert m["manifest_version"] == 3
    forbidden = {"tabs", "<all_urls>", "webRequest", "webRequestBlocking", "cookies"}
    perms = set(m.get("permissions", []))
    assert not (perms & forbidden), f"含禁止权限: {perms & forbidden}"
    assert {"activeTab", "storage", "scripting"} <= perms


def test_no_auto_login_or_purchase_keywords():
    blob = "\n".join(p.read_text(encoding="utf-8") for p in _EXT.glob("*.js"))
    low = blob.lower()
    assert "password" not in low and "document.cookie" not in low
    for kw in ["submitorder", "autofill", "g_recaptcha", "购票请求", "自动下单"]:
        assert kw not in blob, f"扩展含自动购票/登录迹象: {kw}"
