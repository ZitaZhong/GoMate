"""SQLAlchemy 声明式基类。所有模型继承 Base；Alembic autogenerate 依赖其 metadata。"""
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
