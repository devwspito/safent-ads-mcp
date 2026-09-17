"""Rutas REST de lectura de `economics` (contracts/rest-api.md §Economia
unitaria): sin logica de negocio, validan, llaman la fachada, mapean a
JSON compacto (mismo patron que `panel.presentation.rest`)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from safent_ads.economics.application.errors import (
    LagCurveNotFoundError,
    PlatformDivergenceNotFoundError,
    UnitEconomicsProfileNotFoundError,
)
from safent_ads.economics.application.query_service import EconomicsQueryService
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.economics.presentation.serialization import to_json_dict
from safent_ads.iam.presentation.dependencies import require_business_access
from safent_ads.shared.ids import BusinessId

BusinessIdDep = Annotated[uuid.UUID, Depends(require_business_access)]
_NOT_FOUND = HTTPException(status_code=404, detail="No encontrado")


def build_economics_router(service: EconomicsQueryService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/economics", tags=["economics"])

    @router.get("/unit-economics")
    async def get_unit_economics(
        business_id: BusinessIdDep, product_id: uuid.UUID
    ) -> dict[str, Any]:
        return to_json_dict(
            await _or_404(
                service.unit_economics(
                    business_id=BusinessId(business_id), product_id=ProductId(product_id)
                ),
                UnitEconomicsProfileNotFoundError,
            )
        )

    @router.get("/target-cpa")
    async def get_target_cpa(business_id: BusinessIdDep, product_id: uuid.UUID) -> dict[str, Any]:
        return to_json_dict(
            await _or_404(
                service.target_cpa(
                    business_id=BusinessId(business_id), product_id=ProductId(product_id)
                ),
                UnitEconomicsProfileNotFoundError,
            )
        )

    @router.get("/lag-curve")
    async def get_lag_curve(
        business_id: BusinessIdDep, product_id: uuid.UUID, platform: str
    ) -> dict[str, Any]:
        return to_json_dict(
            await _or_404(
                service.lag_curve(
                    business_id=BusinessId(business_id),
                    product_id=ProductId(product_id),
                    platform=platform,
                ),
                LagCurveNotFoundError,
            )
        )

    @router.get("/cohort-projection")
    async def get_cohort_projection(
        business_id: BusinessIdDep,
        product_id: uuid.UUID,
        platform: str,
        observed: int,
        age_days: int,
    ) -> dict[str, Any]:
        return to_json_dict(
            await _or_404(
                service.cohort_projection(
                    business_id=BusinessId(business_id),
                    product_id=ProductId(product_id),
                    platform=platform,
                    observed=observed,
                    age_days=age_days,
                ),
                LagCurveNotFoundError,
            )
        )

    @router.get("/platform-divergence")
    async def get_platform_divergence(
        business_id: BusinessIdDep, platform_account_id: str
    ) -> dict[str, Any]:
        return to_json_dict(
            await _or_404(
                service.platform_divergence(
                    business_id=BusinessId(business_id), platform_account_id=platform_account_id
                ),
                PlatformDivergenceNotFoundError,
            )
        )

    return router


async def _or_404[T](awaitable: Awaitable[T], error_type: type[Exception]) -> T:
    try:
        return await awaitable
    except error_type as exc:
        raise _NOT_FOUND from exc
