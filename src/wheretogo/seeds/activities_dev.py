"""开发用样例活动种子（DD-06 情报流水线上线前的占位）。

生产环境中 `activities` 由 DD-06 写入（读写解耦）；此脚本仅为本地端到端联调提供数据，
活动落在“即将到来的周末”，并用检索服务生成 embedding。
直写绕过 Provenance Guard（DD-03），故所有样例一律 verification_status=estimated 且
evidence.note 标“样例数据”——不得冒充 official/public 确认态（防演示时违反硬 KPI）。
用法：uv run python -m wheretogo.seeds.activities_dev
"""
from __future__ import annotations

import hashlib
from datetime import timedelta

from geoalchemy2 import WKTElement
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db import get_session
# upcoming_weekend 已挪公共模块 domain.timeutil；此处 re-export 兼容既有 import
# （orchestration/nodes.py 仍从本模块取，后续可切到 domain.timeutil）
from ..domain.timeutil import upcoming_weekend
from ..models import Activity
from ..retrieval import RetrievalService


# (title, venue, category, price_text, lng, lat, day_offset, hour, booking_url)
# 定级统一 estimated + evidence.note"样例数据"（见模块 docstring），不逐条标确认态。
_SAMPLES = [
    ("上海博物馆东馆 古埃及文明大展", "上海博物馆东馆", "展览", "¥100", 121.5440, 31.1930,
     0, 10, "https://www.shanghaimuseum.net"),
    ("中华艺术宫 莫奈与印象派大师展", "中华艺术宫", "展览", "¥120", 121.4900, 31.1890,
     0, 13, "https://www.artmuseumonline.org"),
    ("上海大剧院 音乐剧《剧院魅影》", "上海大剧院", "演出", "¥280起", 121.4740, 31.2320,
     0, 19, "https://www.shgtheatre.com"),
    ("上海交响乐团 周末音乐会", "上海交响音乐厅", "演出", "¥180起", 121.4530, 31.2100,
     1, 19, "https://www.shsymphony.com"),
    ("西岸美术馆 蓬皮杜当代艺术展", "西岸美术馆", "展览", "¥150", 121.4650, 31.1830,
     1, 11, "https://www.westbund.com"),
    ("豫园 周末非遗市集", "豫园商城", "市集", "免费", 121.4920, 31.2270,
     0, 15, None),
    ("上海自然博物馆 恐龙亲子探索", "上海自然博物馆", "亲子", "¥30", 121.4600, 31.2360,
     1, 9, "https://www.snhm.org.cn"),
    ("思南公馆 周末城市漫步导览", "思南公馆", "城市漫步", "¥60", 121.4680, 31.2160,
     0, 16, None),
]


def load_sample_activities() -> int:
    svc = RetrievalService()
    sat, _sun = upcoming_weekend()
    n = 0
    with get_session() as s:
        for (title, venue, category, price, lng, lat, day_off, hour, url) in _SAMPLES:
            start_at = sat + timedelta(days=day_off, hours=hour)
            fingerprint = hashlib.sha1(
                f"310000:{title}:{start_at.date()}".encode("utf-8")
            ).hexdigest()
            emb = svc.embed([f"{title} {venue} {category} {price}"])[0]
            values = {
                "fingerprint": fingerprint,
                "title": title,
                "city_code": "310000",
                "venue": venue,
                "location": WKTElement(f"POINT({lng} {lat})", srid=4326),
                "start_at": start_at,
                "end_at": start_at + timedelta(hours=3),
                "price_text": price,
                "booking_url": url,
                "category": category,
                "evidence": {
                    "source_type": "editorial",
                    "source_url": url,
                    "verification_status": "estimated",
                    "confidence": 0.4,
                    "note": "样例数据：开发占位，未经官方核实",
                },
                "verification_status": "estimated",
                "embedding": emb,
                "expires_at": start_at + timedelta(days=2),
            }
            stmt = pg_insert(Activity).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["fingerprint"],
                set_={k: values[k] for k in ("title", "embedding", "start_at", "end_at", "evidence")},
            )
            s.execute(stmt)
            n += 1
    return n


def main() -> None:
    n = load_sample_activities()
    sat, sun = upcoming_weekend()
    print(f"样例活动导入完成：{n} 条（周末窗口 {sat.date()} ~ {sun.date()}，城市 上海/310000）")


if __name__ == "__main__":
    main()
