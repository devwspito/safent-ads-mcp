"""`SqlAssertionReplayRepository` (026, tasks.md T002, contracts/sso.md §7
S-2) contra Postgres real: `jti` unico, purga >24h."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from safent_ads.iam.infrastructure.sql_assertion_replay_repository import (
    SqlAssertionReplayRepository,
)

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
async def clean_session(database_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM sso_assertions_seen"))
        await engine.dispose()


async def test_first_claim_of_a_jti_succeeds(clean_session: AsyncSession) -> None:
    repository = SqlAssertionReplayRepository(clean_session)

    claimed = await repository.claim(jti="jti-unique-1", seen_at=_NOW)

    assert claimed is True


async def test_repeated_jti_is_not_claimed_twice(clean_session: AsyncSession) -> None:
    repository = SqlAssertionReplayRepository(clean_session)
    await repository.claim(jti="jti-unique-2", seen_at=_NOW)

    replay = await repository.claim(jti="jti-unique-2", seen_at=_NOW + timedelta(seconds=1))

    assert replay is False


async def test_purges_rows_older_than_24_hours(clean_session: AsyncSession) -> None:
    repository = SqlAssertionReplayRepository(clean_session)
    await repository.claim(jti="jti-stale", seen_at=_NOW - timedelta(hours=25))

    # Un `jti` distinto, pero el `claim` purga antes de insertar: la fila
    # vieja debe desaparecer de la tabla.
    await repository.claim(jti="jti-fresh", seen_at=_NOW)

    remaining = (
        await clean_session.execute(
            text("SELECT jti FROM sso_assertions_seen ORDER BY jti")
        )
    ).scalars().all()
    assert list(remaining) == ["jti-fresh"]
