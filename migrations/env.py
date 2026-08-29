"""Alembic 运行环境。数据库 URL 从 WTG_ 环境变量注入（Docker 隔离实例）。"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from wheretogo.config import get_settings
from wheretogo.models import Base  # 注册所有表到 Base.metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 用配置里的隔离实例 URL 覆盖 alembic.ini 的空占位
config.set_main_option("sqlalchemy.url", get_settings().sync_db_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
