"""PostgreSQL 专用类型辅助。"""
from __future__ import annotations

from enum import Enum

from sqlalchemy.dialects.postgresql import ENUM as _PGEnum


def pg_enum(py_enum: type[Enum], name: str) -> _PGEnum:
    """引用迁移中已创建的 PG ENUM 类型（不由模型重复创建）。

    存取用枚举 value 字符串（本项目枚举 name==value，两者一致）。
    """
    return _PGEnum(
        py_enum,
        name=name,
        create_type=False,
        values_callable=lambda e: [m.value for m in e],
    )
