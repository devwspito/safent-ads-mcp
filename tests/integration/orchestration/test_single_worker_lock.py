"""`SingleWorkerLock` (security review F2/F3, nit 4): un segundo proceso
`ads-worker` contra el mismo Postgres se niega a arrancar ciclos mientras
el primero siga vivo -- `compose.yaml` fija una sola replica, esto es la
red de seguridad para cuando algo fuera de compose la incumple."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from safent_ads.orchestration.infrastructure.single_worker_lock import SingleWorkerLock

pytestmark = pytest.mark.integration


@pytest.fixture
async def two_worker_engines(
    isolated_database_url: str,
) -> AsyncIterator[tuple[AsyncEngine, AsyncEngine]]:
    """Dos motores independientes contra la MISMA base -- dos procesos
    `ads-worker` distintos nunca comparten conexion, asi que el doble en el
    banco tampoco debe hacerlo (el lock es de SESION, no de aplicacion)."""
    first = create_async_engine(isolated_database_url, pool_pre_ping=True)
    second = create_async_engine(isolated_database_url, pool_pre_ping=True)
    try:
        yield first, second
    finally:
        await first.dispose()
        await second.dispose()


async def test_second_worker_instance_cannot_acquire_the_lock(
    two_worker_engines: tuple[AsyncEngine, AsyncEngine],
) -> None:
    first_engine, second_engine = two_worker_engines
    first_worker = SingleWorkerLock(first_engine)
    second_worker = SingleWorkerLock(second_engine)
    try:
        assert await first_worker.acquire() is True
        assert await second_worker.acquire() is False
    finally:
        await first_worker.release()
        await second_worker.release()


async def test_lock_is_free_again_once_the_first_worker_releases_it(
    two_worker_engines: tuple[AsyncEngine, AsyncEngine],
) -> None:
    first_engine, second_engine = two_worker_engines
    first_worker = SingleWorkerLock(first_engine)
    second_worker = SingleWorkerLock(second_engine)
    try:
        assert await first_worker.acquire() is True
        assert await second_worker.acquire() is False

        await first_worker.release()

        assert await second_worker.acquire() is True
    finally:
        await first_worker.release()
        await second_worker.release()
