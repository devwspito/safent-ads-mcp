"""`GET /economics/customer-value` (spec 027 T017, contracts/crm-link.md
§3): valor de cliente observado sobre `revenue_events`. Router propio,
fuera de `build_economics_router`/`EconomicsQueryService` a proposito: esa
fachada toma sus tres repositorios de `unit_economics_profiles`/
`lag_curve_snapshots`/`platform_divergence_snapshots` ya construidos en
`__init__` (nunca una sesion), y anadir un cuarto repositorio de OTRO
bounded context (`crm`) ahi habria significado tocar su firma y la de
todos sus tests existentes por una unica ruta de lectura."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.crm.application.compute_customer_value import ComputeCustomerValue
from safent_ads.crm.infrastructure.sql_revenue_event_repository import SqlRevenueEventRepository
from safent_ads.iam.presentation.dependencies import require_business_access
from safent_ads.shared.ids import BusinessId

__all__ = ["build_customer_value_router"]

_BusinessIdDep = Annotated[uuid.UUID, Depends(require_business_access)]


def build_customer_value_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/economics", tags=["economics"])

    @router.get("/customer-value")
    async def get_customer_value(
        business_id: _BusinessIdDep, entity_ref: str | None = None
    ) -> dict[str, Any]:
        async with session_factory() as session:
            value = await ComputeCustomerValue(
                revenue_events=SqlRevenueEventRepository(session)
            ).execute(business_id=BusinessId(business_id), entity_ref=entity_ref)
        return {
            "cohort_size": value.cohort_size,
            "observed_contribution_minor": value.observed_contribution_minor,
            "currency": value.currency,
            "projected_contribution_minor": value.projected_contribution_minor,
            "maturity": value.maturity,
            "horizon_days": value.horizon_days,
            "is_provisional": value.is_provisional,
            "no_number_reason": value.no_number_reason,
        }

    return router
