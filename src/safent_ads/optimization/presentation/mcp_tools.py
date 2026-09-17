"""Las 4 herramientas P1 restantes de `optimization` (profitability-
engine.md §8): `get_marginal_roas`, `diagnose_entity`, `simulate_spend_change`,
`propose_reallocation_plan` (las 5 de economia unitaria ya viven en
`economics.presentation.mcp_tools`).

`build_optimization_tool_specs(service)` es el unico punto de montaje: la
lane que cablea `mcp/` importa esto y adapta cada `ToolSpec` a un
`ToolDefinition` (ver docstring de `tool_spec.py`)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from safent_ads.economics.domain.money import Money
from safent_ads.optimization.application.errors import (
    ContributionMarginNotFoundError,
    DiagnosisMetricsNotFoundError,
    MarginalEstimateNotFoundError,
    NoReallocationCandidatesError,
    ReallocationVetoedError,
    ResponseCurveNotFoundError,
)
from safent_ads.optimization.application.query_service import OptimizationQueryService
from safent_ads.optimization.presentation import args as a
from safent_ads.optimization.presentation.serialization import to_json_dict
from safent_ads.optimization.presentation.tool_spec import ToolSpec
from safent_ads.shared.ids import BusinessId, EntityRef

_NOT_FOUND_CODE = "ENTITY_NOT_FOUND"
_INVALID_ARGUMENT_CODE = "INVALID_ARGUMENT"
_VETOED_CODE = "REALLOCATION_VETOED"


def build_optimization_tool_specs(service: OptimizationQueryService) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="get_marginal_roas",
            description="Contribucion marginal por euro gastado de una entidad: valor, IC, metodo.",
            args_model=a.GetMarginalRoasArgs,
            handler=_get_marginal_roas(service),
        ),
        ToolSpec(
            name="diagnose_entity",
            description="Camino completo de diagnostico estructural (9 nodos) de una entidad.",
            args_model=a.DiagnoseEntityArgs,
            handler=_diagnose_entity(service),
        ),
        ToolSpec(
            name="simulate_spend_change",
            description="Delta de contribucion, IC y out_of_support de un escenario de gasto.",
            args_model=a.SimulateSpendChangeArgs,
            handler=_simulate_spend_change(service),
        ),
        ToolSpec(
            name="propose_reallocation_plan",
            description=(
                "Propone bajada AUTO y subida con aprobacion entre el donante y el receptor "
                "de menor/mayor contribucion marginal."
            ),
            args_model=a.ProposeReallocationPlanArgs,
            handler=_propose_reallocation_plan(service),
            verb_kind="proposal",
        ),
    ]


def _get_marginal_roas(service: OptimizationQueryService) -> Any:
    async def handler(args: a.GetMarginalRoasArgs) -> dict[str, Any]:
        try:
            view = await service.marginal_roas(
                business_id=BusinessId.parse(args.business_id),
                entity_ref=EntityRef.parse(args.entity_ref),
            )
        except MarginalEstimateNotFoundError:
            return _not_found("sin estimacion de contribucion marginal materializada")
        return to_json_dict(view)

    return handler


def _diagnose_entity(service: OptimizationQueryService) -> Any:
    async def handler(args: a.DiagnoseEntityArgs) -> dict[str, Any]:
        try:
            view = await service.diagnose_entity(
                business_id=BusinessId.parse(args.business_id),
                entity_ref=EntityRef.parse(args.entity_ref),
            )
        except DiagnosisMetricsNotFoundError:
            return _not_found("sin metricas suficientes para diagnosticar esta entidad")
        return to_json_dict(view)

    return handler


def _simulate_spend_change(service: OptimizationQueryService) -> Any:
    async def handler(args: a.SimulateSpendChangeArgs) -> dict[str, Any]:
        try:
            spend = Money.of(Decimal(args.current_daily_spend_amount))
        except InvalidOperation:
            return _invalid_argument("current_daily_spend_amount no es un importe valido")
        try:
            view = await service.simulate_spend_change(
                business_id=BusinessId.parse(args.business_id),
                entity_ref=EntityRef.parse(args.entity_ref),
                product_id=args.product_id,
                current_daily_spend=spend,
                spend_multiplier=args.spend_multiplier,
            )
        except ResponseCurveNotFoundError:
            return _not_found("sin curva de respuesta materializada para esta entidad")
        except ContributionMarginNotFoundError:
            return _not_found("sin perfil de economia unitaria para este producto")
        return to_json_dict(view)

    return handler


def _propose_reallocation_plan(service: OptimizationQueryService) -> Any:
    async def handler(args: a.ProposeReallocationPlanArgs) -> dict[str, Any]:
        try:
            view = await service.propose_reallocation_plan(
                business_id=BusinessId.parse(args.business_id)
            )
        except NoReallocationCandidatesError:
            return _not_found("menos de dos candidatos elegibles para comparar")
        except ReallocationVetoedError as exc:
            return _vetoed(exc.reason)
        return to_json_dict(view)

    return handler


def _not_found(reason: str) -> dict[str, Any]:
    return {"error": {"code": _NOT_FOUND_CODE, "message": reason}}


def _invalid_argument(reason: str) -> dict[str, Any]:
    return {"error": {"code": _INVALID_ARGUMENT_CODE, "message": reason}}


def _vetoed(reason: str) -> dict[str, Any]:
    return {"error": {"code": _VETOED_CODE, "message": reason}}
