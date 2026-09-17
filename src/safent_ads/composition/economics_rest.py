"""Cablea `economics/presentation/rest.py` a Postgres real: mismo patron que
`panel.infrastructure.sql_read_model.RequestScopedPanelReadPort` (una sesion
por llamada via `container.session_factory`), pero un nivel mas abajo -- a
nivel de puerto, no de fachada entera -- porque `EconomicsQueryService` toma
sus tres repositorios ya construidos en `__init__` (no una sesion), y cada
`Sql*Repository` de `economics/infrastructure/sql_repositories.py` esta
atado a UNA `AsyncSession`. Se construye `EconomicsQueryService` una sola
vez al arrancar la app con estos tres adaptadores; cada llamada de metodo
abre y cierra su propia sesion por debajo, asi que no hay estado compartido
entre peticiones concurrentes.

`optimization/presentation/rest.py` se queda sin montar a proposito:
`OptimizationQueryService` necesita `DiagnosisMetricsPort`/
`ContributionMarginPort`/`ReallocationCandidateRepository`/
`ReallocationProposalPort`, y solo existen implementaciones en memoria
(`optimization/testing/in_memory_repositories.py`) -- no hay adaptador SQL
real que conectar todavia. Montarlo hoy serviria datos falsos disfrazados
de reales, que es justo lo que este cableado existe para evitar. Escalar a
`tech-lead`/`backend-engineer`: falta disenar esos cuatro adaptadores sobre
`metrics`/`economics`/`optimization` (trabajo de dominio, no de cableado)."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.economics.application.query_service import EconomicsQueryService
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.economics.domain.lag_curve import LagCurve
from safent_ads.economics.domain.platform_divergence import PlatformDivergence
from safent_ads.economics.domain.unit_economics import UnitEconomicsProfile
from safent_ads.economics.infrastructure.sql_repositories import (
    SqlLagCurveRepository,
    SqlPlatformDivergenceRepository,
    SqlUnitEconomicsProfileRepository,
)
from safent_ads.economics.presentation.rest import build_economics_router
from safent_ads.shared.clock import Clock, SystemClock
from safent_ads.shared.ids import BusinessId

__all__ = ["build_economics_query_service", "build_economics_read_router"]


class _RequestScopedUnitEconomicsProfiles:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_current(
        self, *, business_id: BusinessId, product_id: ProductId, as_of: date
    ) -> UnitEconomicsProfile | None:
        async with self._session_factory() as session:
            return await SqlUnitEconomicsProfileRepository(session).get_current(
                business_id=business_id, product_id=product_id, as_of=as_of
            )

    async def list_versions(
        self, *, business_id: BusinessId, product_id: ProductId
    ) -> list[UnitEconomicsProfile]:
        async with self._session_factory() as session:
            return await SqlUnitEconomicsProfileRepository(session).list_versions(
                business_id=business_id, product_id=product_id
            )

    async def save(self, profile: UnitEconomicsProfile) -> None:
        async with self._session_factory() as session:
            await SqlUnitEconomicsProfileRepository(session).save(profile)
            await session.commit()


class _RequestScopedLagCurves:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_current(
        self, *, business_id: BusinessId, product_id: ProductId, platform: str
    ) -> LagCurve | None:
        async with self._session_factory() as session:
            return await SqlLagCurveRepository(session).get_current(
                business_id=business_id, product_id=product_id, platform=platform
            )

    async def save(
        self, *, business_id: BusinessId, product_id: ProductId, platform: str, curve: LagCurve
    ) -> None:
        async with self._session_factory() as session:
            await SqlLagCurveRepository(session).save(
                business_id=business_id, product_id=product_id, platform=platform, curve=curve
            )
            await session.commit()


class _RequestScopedPlatformDivergences:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_latest(
        self, *, business_id: BusinessId, platform_account_id: str
    ) -> PlatformDivergence | None:
        async with self._session_factory() as session:
            return await SqlPlatformDivergenceRepository(session).get_latest(
                business_id=business_id, platform_account_id=platform_account_id
            )

    async def get_previous(
        self, *, business_id: BusinessId, platform_account_id: str
    ) -> PlatformDivergence | None:
        async with self._session_factory() as session:
            return await SqlPlatformDivergenceRepository(session).get_previous(
                business_id=business_id, platform_account_id=platform_account_id
            )

    async def save(
        self, *, business_id: BusinessId, platform_account_id: str, divergence: PlatformDivergence
    ) -> None:
        async with self._session_factory() as session:
            await SqlPlatformDivergenceRepository(session).save(
                business_id=business_id,
                platform_account_id=platform_account_id,
                divergence=divergence,
            )
            await session.commit()


def build_economics_query_service(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock | None = None
) -> EconomicsQueryService:
    """Unico punto de construccion sobre Postgres real: `build_economics_
    read_router` (REST) y `composition/app.py::_build_mcp_registry_and_
    dispatcher` (MCP, B-1 checklists/final-review.md) comparten esto para
    no duplicar los tres adaptadores `_RequestScoped*` de arriba -- cada
    uno abre y cierra su propia sesion por llamada, asi que dos instancias
    de `EconomicsQueryService` (una por superficie) no comparten estado."""
    return EconomicsQueryService(
        profiles=_RequestScopedUnitEconomicsProfiles(session_factory),
        lag_curves=_RequestScopedLagCurves(session_factory),
        divergences=_RequestScopedPlatformDivergences(session_factory),
        clock=clock or SystemClock(),
    )


def build_economics_read_router(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock | None = None
) -> APIRouter:
    return build_economics_router(build_economics_query_service(session_factory, clock))
