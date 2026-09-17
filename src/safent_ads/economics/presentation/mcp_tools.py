"""Las 5 herramientas de lectura de `economics` (profitability-engine.md
§8 P1): `get_unit_economics`, `get_target_cpa`, `get_attribution_lag_curve`,
`get_cohort_projection`, `get_platform_divergence`.

`build_economics_tool_specs(service)` es el unico punto de montaje: la lane
que cablea `mcp/` importa esto y adapta cada `ToolSpec` a un
`ToolDefinition` (ver docstring de `tool_spec.py`)."""

from __future__ import annotations

from typing import Any

from safent_ads.economics.application.errors import (
    LagCurveNotFoundError,
    PlatformDivergenceNotFoundError,
    UnitEconomicsProfileNotFoundError,
)
from safent_ads.economics.application.query_service import EconomicsQueryService
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.economics.presentation import args as a
from safent_ads.economics.presentation.serialization import to_json_dict
from safent_ads.economics.presentation.tool_spec import ToolSpec
from safent_ads.shared.ids import BusinessId

# Errores tipados (contracts/mcp-tools.md regla 6): capacidad reportada,
# nunca fingida -- "sin datos" es una respuesta valida, no una excepcion
# que llega al agente como traza.
_NOT_FOUND_CODE = "ENTITY_NOT_FOUND"


def build_economics_tool_specs(service: EconomicsQueryService) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="get_unit_economics",
            description="Contribucion y objetivos de un producto: dato vs inferencia.",
            args_model=a.GetUnitEconomicsArgs,
            handler=_get_unit_economics(service),
        ),
        ToolSpec(
            name="get_target_cpa",
            description="Coste objetivo por conversion y por lead de un producto.",
            args_model=a.GetTargetCpaArgs,
            handler=_get_target_cpa(service),
        ),
        ToolSpec(
            name="get_attribution_lag_curve",
            description="Curva de rezago de conversion F(d) de un producto x plataforma.",
            args_model=a.GetAttributionLagCurveArgs,
            handler=_get_attribution_lag_curve(service),
        ),
        ToolSpec(
            name="get_cohort_projection",
            description="Observado, proyectado, madurez e intervalo de una cohorte en vuelo.",
            args_model=a.GetCohortProjectionArgs,
            handler=_get_cohort_projection(service),
        ),
        ToolSpec(
            name="get_platform_divergence",
            description="Divergencia CRM vs plataforma (delta_hat) de una cuenta.",
            args_model=a.GetPlatformDivergenceArgs,
            handler=_get_platform_divergence(service),
        ),
    ]


def _get_unit_economics(service: EconomicsQueryService) -> Any:
    async def handler(args: a.GetUnitEconomicsArgs) -> dict[str, Any]:
        try:
            view = await service.unit_economics(
                business_id=BusinessId.parse(args.business_id),
                product_id=ProductId.parse(args.product_id),
            )
        except UnitEconomicsProfileNotFoundError:
            return _not_found("no hay perfil de economia unitaria para este producto")
        return to_json_dict(view)

    return handler


def _get_target_cpa(service: EconomicsQueryService) -> Any:
    async def handler(args: a.GetTargetCpaArgs) -> dict[str, Any]:
        try:
            view = await service.target_cpa(
                business_id=BusinessId.parse(args.business_id),
                product_id=ProductId.parse(args.product_id),
            )
        except UnitEconomicsProfileNotFoundError:
            return _not_found("no hay perfil de economia unitaria para este producto")
        return to_json_dict(view)

    return handler


def _get_attribution_lag_curve(service: EconomicsQueryService) -> Any:
    async def handler(args: a.GetAttributionLagCurveArgs) -> dict[str, Any]:
        try:
            view = await service.lag_curve(
                business_id=BusinessId.parse(args.business_id),
                product_id=ProductId.parse(args.product_id),
                platform=args.platform,
            )
        except LagCurveNotFoundError:
            return _not_found("sin curva de rezago materializada para esta combinacion")
        return to_json_dict(view)

    return handler


def _get_cohort_projection(service: EconomicsQueryService) -> Any:
    async def handler(args: a.GetCohortProjectionArgs) -> dict[str, Any]:
        try:
            view = await service.cohort_projection(
                business_id=BusinessId.parse(args.business_id),
                product_id=ProductId.parse(args.product_id),
                platform=args.platform,
                observed=args.observed,
                age_days=args.age_days,
            )
        except LagCurveNotFoundError:
            return _not_found("sin curva de rezago materializada para esta combinacion")
        return to_json_dict(view)

    return handler


def _get_platform_divergence(service: EconomicsQueryService) -> Any:
    async def handler(args: a.GetPlatformDivergenceArgs) -> dict[str, Any]:
        try:
            view = await service.platform_divergence(
                business_id=BusinessId.parse(args.business_id),
                platform_account_id=args.platform_account_id,
            )
        except PlatformDivergenceNotFoundError:
            return _not_found("sin snapshot de divergencia para esta cuenta")
        return to_json_dict(view)

    return handler


def _not_found(reason: str) -> dict[str, Any]:
    return {"error": {"code": _NOT_FOUND_CODE, "message": reason}}
