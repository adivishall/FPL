"""Alembic environment: migrations are generated from fpl_storage.models (single source)."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from fpl_storage.models import Base

config = context.config
url = os.environ.get("FPL_DATABASE_URL") or config.get_main_option("sqlalchemy.url")
if not url:
    raise RuntimeError("FPL_DATABASE_URL must be set to run migrations")
config.set_main_option("sqlalchemy.url", url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
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
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
