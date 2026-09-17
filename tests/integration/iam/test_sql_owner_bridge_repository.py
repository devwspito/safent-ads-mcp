"""`SqlOwnerBridgeRepository` (026, tasks.md T002, contracts/sso.md §4)
contra Postgres real. Usa `isolated_iam_database_url`, exclusiva del módulo,
porque la resolución de propietario no filtra por `business_id`. Limpia
sus filas sin TRUNCATE CASCADE, que atravesaría el historial de aprobaciones
inmutable mediante las nuevas FK de propiedad de conexión."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from safent_ads.iam.application.errors import OwnerBoundElsewhereError
from safent_ads.iam.application.ports import OwnerBridgeResolution
from safent_ads.iam.infrastructure.sql_owner_bridge_repository import SqlOwnerBridgeRepository

pytestmark = pytest.mark.integration


@pytest.fixture
async def clean_session(isolated_iam_database_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(isolated_iam_database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM sessions"))
        await connection.execute(text("DELETE FROM owners"))
    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM sessions"))
            await connection.execute(text("DELETE FROM owners"))
        await engine.dispose()


async def _count_owners(session: AsyncSession) -> int:
    result = await session.execute(text("SELECT count(*) FROM owners"))
    return int(result.scalar_one())


async def test_no_owner_creates_one_bound_to_the_subject(clean_session: AsyncSession) -> None:
    repository = SqlOwnerBridgeRepository(clean_session)

    resolved = await repository.resolve_for_subject("sub-first-boot")

    assert resolved.resolution is OwnerBridgeResolution.CREATED
    assert await _count_owners(clean_session) == 1


async def test_unbound_owner_gets_bound_tofu(clean_session: AsyncSession) -> None:
    await clean_session.execute(
        text(
            "INSERT INTO owners (id, email, password_hash) VALUES (gen_random_uuid(), "
            "'unbound@safent.example', 'irrelevant')"
        )
    )
    await clean_session.commit()
    repository = SqlOwnerBridgeRepository(clean_session)

    resolved = await repository.resolve_for_subject("sub-tofu")

    assert resolved.resolution is OwnerBridgeResolution.BOUND
    row = (
        (
            await clean_session.execute(
                text("SELECT bridge_subject FROM owners WHERE id = :id"),
                {"id": str(resolved.owner_id)},
            )
        )
        .mappings()
        .one()
    )
    assert row["bridge_subject"] == "sub-tofu"


async def test_owner_already_bound_to_the_same_subject_passes_through(
    clean_session: AsyncSession,
) -> None:
    repository = SqlOwnerBridgeRepository(clean_session)
    first = await repository.resolve_for_subject("sub-stable")

    second = await repository.resolve_for_subject("sub-stable")

    assert second.owner_id == first.owner_id
    assert second.resolution is OwnerBridgeResolution.ALREADY_BOUND
    assert await _count_owners(clean_session) == 1


async def test_owner_bound_to_a_different_subject_is_rejected(
    clean_session: AsyncSession,
) -> None:
    repository = SqlOwnerBridgeRepository(clean_session)
    await repository.resolve_for_subject("sub-owner")

    with pytest.raises(OwnerBoundElsewhereError):
        await repository.resolve_for_subject("sub-attacker")
