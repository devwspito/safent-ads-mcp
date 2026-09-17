"""`SqlLoginAttemptRepository` (0001_bootstrap.py, threat-model.md C-25)
contra Postgres real.

Code review 17-sep (item 2): antes de que `_client_ip` delegara en
`shared/net/client_ip.py`, una peticion sin `request.client` (raro, pero
posible) llegaba aqui con el literal `"unknown"` -- `login_attempts.
ip_address` es `INET`, y Postgres rechaza ese texto con un `DataError`
(500). `ip_address=None` es lo que `resolve_client_ip` devuelve en ese
caso, y esta suite prueba que la columna, de verdad nullable, lo acepta."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from safent_ads.iam.infrastructure.sql_login_attempt_repository import SqlLoginAttemptRepository

pytestmark = pytest.mark.integration

_EMAIL = "sin-ip@example.com"


async def _wipe(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM login_attempts WHERE email = :email"), {"email": _EMAIL}
        )


@pytest.fixture
async def engine(isolated_iam_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(isolated_iam_database_url, pool_pre_ping=True)
    await _wipe(engine)
    try:
        yield engine
    finally:
        await _wipe(engine)
        await engine.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()


async def test_recording_an_attempt_with_no_resolvable_ip_does_not_raise(
    db_session: AsyncSession,
) -> None:
    repository = SqlLoginAttemptRepository(db_session)

    await repository.record(email=_EMAIL, succeeded=False, ip_address=None)

    failures = await repository.count_recent_failures(
        email=_EMAIL, ip_address=None, since=datetime.now(UTC) - timedelta(minutes=1)
    )
    assert failures == 0


async def test_a_none_ip_never_matches_a_stored_real_ip(db_session: AsyncSession) -> None:
    """Comparar `NULL` con cualquier cosa nunca es verdadero en SQL: una
    fila sin IP conocida no infla el contador de bloqueo de ninguna IP
    real, ni al reves."""
    repository = SqlLoginAttemptRepository(db_session)
    since = datetime.now(UTC) - timedelta(minutes=1)

    await repository.record(email=_EMAIL, succeeded=False, ip_address=None)
    await repository.record(email=_EMAIL, succeeded=False, ip_address="203.0.113.9")

    assert await repository.count_recent_failures(
        email=_EMAIL, ip_address="203.0.113.9", since=since
    ) == 1
    assert await repository.count_recent_failures(email=_EMAIL, ip_address=None, since=since) == 0
