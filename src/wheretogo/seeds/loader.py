"""种子数据加载（DD-01 §10.2）。

用法：uv run python -m wheretogo.seeds.loader [path/to/cities.yaml]
幂等：city_playbook 按 city_code upsert；source_registry 按 (name, entry_url) 去重。
连接的是 Docker 隔离实例（绝不触碰本地现有 PostgreSQL）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from geoalchemy2 import WKTElement
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db import get_session
from ..models import CityPlaybook, SourceRegistry

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SEED = _PROJECT_ROOT / "seeds" / "cities.yaml"


def _point(node: dict | None) -> WKTElement | None:
    if not node:
        return None
    return WKTElement(f"POINT({node['lng']} {node['lat']})", srid=4326)


def load_seed(path: Path | str = DEFAULT_SEED) -> dict[str, int]:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cities = data.get("cities", [])
    sources = data.get("sources", [])

    n_city, n_src = 0, 0
    with get_session() as s:
        for c in cities:
            values = {
                "city_code": c["city_code"],
                "name": c["name"],
                "center": _point(c.get("center")),
                "stations": c.get("stations"),
                "lodging_areas": c.get("lodging_areas"),
                "hubs": c.get("hubs"),
                "transit_notes": c.get("transit_notes"),
                "weekend_tags": c.get("weekend_tags"),
                "seasonal_risk": c.get("seasonal_risk"),
            }
            stmt = pg_insert(CityPlaybook).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["city_code"],
                set_={k: v for k, v in values.items() if k != "city_code"},
            )
            s.execute(stmt)
            n_city += 1

        for src in sources:
            exists = s.scalar(
                select(SourceRegistry.id).where(
                    SourceRegistry.name == src["name"],
                    SourceRegistry.entry_url == src["entry_url"],
                )
            )
            if exists:
                continue
            s.add(
                SourceRegistry(
                    name=src["name"],
                    city_code=src.get("city_code"),
                    source_type=src["source_type"],
                    entry_url=src["entry_url"],
                    parser_kind=src.get("parser_kind"),
                    trust_level=src.get("trust_level", 3),
                )
            )
            n_src += 1

    return {"cities": n_city, "sources": n_src}


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SEED
    result = load_seed(path)
    print(f"种子导入完成：city_playbook upsert={result['cities']}，source_registry 新增={result['sources']}")


if __name__ == "__main__":
    main()
