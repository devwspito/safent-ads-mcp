"""Runner async de Alembic. Las migraciones son SQL de mano (`op.execute`),
no autogenerate: data-model.md exige *imperative mapping*, sin tablas
SQLAlchemy declarativas que el ORM pudiera filtrar hacia el dominio."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _database_url() -> str:
    url = os.environ.get("ADS_DATABASE_URL")
    if not url:
        raise RuntimeError("ADS_DATABASE_URL no definido: no se puede migrar.")
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations_sync(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable: AsyncEngine = create_async_engine(_database_url())
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(_run_migrations_sync)
    finally:
        # A rejected migration must not leave a pooled connection alive.
        await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
