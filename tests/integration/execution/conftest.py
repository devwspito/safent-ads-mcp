"""Escenarios CONFIRMADOS para los casos de `execution` que necesitan mas de
una conexion a la vez.

El resto de la suite trabaja sobre una transaccion que se deshace al final
(`rolled_back_session`), pero eso no sirve aqui: dos trabajadores compitiendo
por la misma fila, o un freno que se enciende a media ejecucion, son
justamente casos en los que una sesion tiene que VER lo que otra confirmo.
Por eso se escribe de verdad y se limpia a mano al terminar."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from tests.conftest import rolled_back_session
from tests.contracts.execution.conftest import AuthorizedProposal, seed_authorized_proposal

# Al terminar solo se retiran las ejecuciones (y sus apuntes de gasto, que
# las referencian): son las unicas filas que las consultas GLOBALES de este
# lane pueden ver de un escenario ajeno -- el reclamo de la cola no filtra
# por negocio. El resto del escenario se queda: `approvals` es solo-anexable
# por trigger (0008_proposals), esta base es exclusiva de la suite y cada
# escenario estrena negocio, cuenta y entidad.
_PURGE = (
    "DELETE FROM spend_ledger WHERE business_id = :business_id",
    "DELETE FROM executions WHERE business_id = :business_id",
    "DELETE FROM emergency_brakes WHERE business_id = :business_id"
    " OR platform_account_id IN"
    " (SELECT id FROM platform_accounts WHERE business_id = :business_id)",
)


@dataclass(frozen=True, slots=True)
class CommittedScenario:
    """Datos ya confirmados en la base y el motor con el que abrir tantas
    sesiones independientes como pida el caso."""

    engine: AsyncEngine
    context: AuthorizedProposal

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Sesion con conexion propia y transaccion propia: lo que escriba se
        deshace al salir salvo que el caso confirme explicitamente."""
        async with self.engine.connect() as connection:
            session = AsyncSession(bind=connection, expire_on_commit=False)
            try:
                yield session
            finally:
                await session.rollback()
                await session.close()


@asynccontextmanager
async def committed_scenario(database_url: str) -> AsyncIterator[CommittedScenario]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    scenario = CommittedScenario(engine=engine, context=await _seed(engine))
    try:
        yield scenario
    finally:
        await _purge(scenario)
        await engine.dispose()


async def _seed(engine: AsyncEngine) -> AuthorizedProposal:
    async with engine.connect() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        context = await seed_authorized_proposal(session)
        await session.commit()
        await session.close()
        return context


async def _purge(scenario: CommittedScenario) -> None:
    async with scenario.engine.begin() as cleanup:
        for statement in _PURGE:
            await cleanup.execute(
                text(statement), {"business_id": scenario.context.business_id.value}
            )


@pytest.fixture
async def isolated_session(isolated_database_url: str) -> AsyncIterator[AsyncSession]:
    """Como `db_session`, pero sobre la base sin filas de nadie mas: el
    reclamo de la cola y las sumas de gasto no filtran por negocio."""
    async with rolled_back_session(isolated_database_url) as session:
        yield session
