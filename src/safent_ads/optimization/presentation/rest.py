"""Rutas REST de `optimization` (contracts/rest-api.md §Economia y
optimizacion): sin logica de negocio, validan, llaman la fachada, mapean a
JSON compacto (mismo patron que `economics.presentation.rest`).

`POST /reallocation-plan` es la unica mutacion: crea dos `PropuestaDeAccion`
ligadas via el puerto de salida, nunca escribe en plataforma."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.economics.domain.money import Money
from safent_ads.iam.presentation.dependencies import require_business_access
from safent_ads.optimization.application.errors import (
    ContributionMarginNotFoundError,
    DiagnosisMetricsNotFoundError,
    MarginalEstimateNotFoundError,
    NoReallocationCandidatesError,
    ReallocationVetoedError,
    ResponseCurveNotFoundError,
)
from safent_ads.optimization.application.query_service import OptimizationQueryService
from safent_ads.optimization.infrastructure.request_scoped_repositories import (
    RequestScopedContributionMargins,
    RequestScopedDiagnosisMetrics,
    RequestScopedMarginalEstimates,
    RequestScopedReallocationCandidates,
    RequestScopedReallocationProposals,
    RequestScopedResponseCurves,
)
from safent_ads.optimization.presentation.serialization import to_json_dict
from safent_ads.shared.clock import Clock, SystemClock
from safent_ads.shared.ids import BusinessId, EntityRef, EntityRefFormatError

BusinessIdDep = Annotated[uuid.UUID, Depends(require_business_access)]
_NOT_FOUND = HTTPException(status_code=404, detail="No encontrado")
_INVALID_AMOUNT = HTTPException(status_code=422, detail="current_daily_spend_amount invalido")


def build_optimization_router(service: OptimizationQueryService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/optimization", tags=["optimization"])

    @router.get("/marginal-roas")
    async def get_marginal_roas(business_id: BusinessIdDep, entity_ref: str) -> dict[str, Any]:
        return to_json_dict(
            await _or_404(
                service.marginal_roas(
                    business_id=BusinessId(business_id), entity_ref=_parse_entity_ref(entity_ref)
                ),
                MarginalEstimateNotFoundError,
            )
        )

    @router.get("/diagnosis")
    async def get_diagnosis(business_id: BusinessIdDep, entity_ref: str) -> dict[str, Any]:
        return to_json_dict(
            await _or_404(
                service.diagnose_entity(
                    business_id=BusinessId(business_id), entity_ref=_parse_entity_ref(entity_ref)
                ),
                DiagnosisMetricsNotFoundError,
            )
        )

    @router.get("/spend-simulation")
    async def get_spend_simulation(
        business_id: BusinessIdDep,
        entity_ref: str,
        product_id: str,
        current_daily_spend_amount: str,
        spend_multiplier: float,
    ) -> dict[str, Any]:
        spend = _parse_money(current_daily_spend_amount)
        try:
            view = await service.simulate_spend_change(
                business_id=BusinessId(business_id),
                entity_ref=_parse_entity_ref(entity_ref),
                product_id=product_id,
                current_daily_spend=spend,
                spend_multiplier=spend_multiplier,
            )
        except (ResponseCurveNotFoundError, ContributionMarginNotFoundError) as exc:
            raise _NOT_FOUND from exc
        return to_json_dict(view)

    @router.post("/reallocation-plan", status_code=201)
    async def post_reallocation_plan(business_id: BusinessIdDep) -> dict[str, Any]:
        try:
            view = await service.propose_reallocation_plan(business_id=BusinessId(business_id))
        except NoReallocationCandidatesError as exc:
            raise HTTPException(status_code=404, detail="NO_CANDIDATES") from exc
        except ReallocationVetoedError as exc:
            raise HTTPException(status_code=409, detail="REALLOCATION_VETOED") from exc
        return to_json_dict(view)

    return router


def build_optimization_router_over_sql(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock | None = None
) -> APIRouter:
    """Cablea los 6 puertos de `OptimizationQueryService` sobre Postgres
    real (mismo patron que `composition/economics_rest.py::
    build_economics_read_router`, aqui dentro de `optimization/
    presentation/` para que `composition/app.py` solo necesite una linea
    de montaje: `app.include_router(build_optimization_router_over_sql(...))`.
    Los 4 puertos que antes solo tenian doble en memoria
    (`ReallocationCandidateRepository`, `DiagnosisMetricsPort`,
    `ContributionMarginPort`, `ReallocationProposalPort`) ya tienen
    adaptador SQL real en `optimization/infrastructure/`."""
    resolved_clock = clock or SystemClock()
    service = OptimizationQueryService(
        marginal_estimates=RequestScopedMarginalEstimates(session_factory),
        diagnosis_metrics=RequestScopedDiagnosisMetrics(session_factory, resolved_clock),
        response_curves=RequestScopedResponseCurves(session_factory),
        contribution_margins=RequestScopedContributionMargins(session_factory, resolved_clock),
        reallocation_candidates=RequestScopedReallocationCandidates(session_factory),
        reallocation_proposals=RequestScopedReallocationProposals(session_factory, resolved_clock),
        clock=resolved_clock,
    )
    return build_optimization_router(service)


def _parse_entity_ref(raw: str) -> EntityRef:
    try:
        return EntityRef.parse(raw)
    except EntityRefFormatError as exc:
        raise HTTPException(status_code=422, detail="entity_ref invalido") from exc


def _parse_money(raw: str) -> Money:
    try:
        return Money.of(Decimal(raw))
    except InvalidOperation as exc:
        raise _INVALID_AMOUNT from exc


async def _or_404[T](awaitable: Awaitable[T], error_type: type[Exception]) -> T:
    try:
        return await awaitable
    except error_type as exc:
        raise _NOT_FOUND from exc
