"""Adapta las 5 `ToolSpec` de lectura de `economics` (P1, profitability-
engine.md §8: `get_unit_economics`, `get_target_cpa`,
`get_attribution_lag_curve`, `get_cohort_projection`,
`get_platform_divergence`) al `ToolRegistry` real -- el hueco que
`checklists/final-review.md` B-1 describe: los `ToolSpec` existen y estan
probados en `economics/presentation/mcp_tools.py`, pero ningun modulo de
`composition/` los importaba.

`economics` no importa `mcp` (plan.md §4): sus `ToolSpec` declaran
argumentos sobre `economics.presentation.args.EconomicsToolArgs`, que no
hereda de `mcp.presentation.args.ToolArgs` -- el unico tipo que satisface
el limite generico `ToolDefinition[ArgsT: ToolArgs]`. Este modulo (mismo
criterio de aislamiento que `experiment_tools.py`/`opportunity_tools.py`)
declara su propia copia de los argumentos que SI hereda de `ToolArgs`
(mismas invariantes de seguridad: `extra=forbid`, sin URLs libres,
`business_id` obligatorio) y reconstruye la instancia del `args_model`
original de `economics` antes de llamar a `spec.handler` -- la logica de
negocio y el mapeo de errores tipados (`ENTITY_NOT_FOUND`) siguen viviendo,
intactos y probados, en `economics/presentation/mcp_tools.py`."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from pydantic import Field

from safent_ads.economics.application.query_service import EconomicsQueryService
from safent_ads.economics.presentation import args as economics_args
from safent_ads.economics.presentation.mcp_tools import build_economics_tool_specs
from safent_ads.economics.presentation.tool_spec import ToolSpec
from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.application.dto import PlatformCode
from safent_ads.mcp.presentation.args import BusinessId, ToolArgs
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition

__all__ = ["build_economics_tool_definitions"]

type Handler[ArgsT, ReturnT] = Callable[[ArgsT, CallerScope], Awaitable[ReturnT]]

_UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
ProductIdStr = Annotated[str, Field(pattern=_UUID_PATTERN)]
PlatformAccountIdStr = Annotated[str, Field(pattern=_UUID_PATTERN)]


class GetUnitEconomicsArgs(ToolArgs):
    business_id: BusinessId
    product_id: ProductIdStr


class GetTargetCpaArgs(ToolArgs):
    business_id: BusinessId
    product_id: ProductIdStr


class GetAttributionLagCurveArgs(ToolArgs):
    business_id: BusinessId
    product_id: ProductIdStr
    platform: PlatformCode


class GetCohortProjectionArgs(ToolArgs):
    business_id: BusinessId
    product_id: ProductIdStr
    platform: PlatformCode
    observed: int = Field(ge=0)
    age_days: int = Field(ge=0)


class GetPlatformDivergenceArgs(ToolArgs):
    business_id: BusinessId
    platform_account_id: PlatformAccountIdStr


def build_economics_tool_definitions(
    service: EconomicsQueryService,
) -> list[ToolDefinition[Any]]:
    specs = {spec.name: spec for spec in build_economics_tool_specs(service)}
    return [
        _adapt(
            specs["get_unit_economics"],
            GetUnitEconomicsArgs,
            lambda args: economics_args.GetUnitEconomicsArgs(
                business_id=args.business_id, product_id=args.product_id
            ),
        ),
        _adapt(
            specs["get_target_cpa"],
            GetTargetCpaArgs,
            lambda args: economics_args.GetTargetCpaArgs(
                business_id=args.business_id, product_id=args.product_id
            ),
        ),
        _adapt(
            specs["get_attribution_lag_curve"],
            GetAttributionLagCurveArgs,
            lambda args: economics_args.GetAttributionLagCurveArgs(
                business_id=args.business_id,
                product_id=args.product_id,
                platform=args.platform.value,
            ),
        ),
        _adapt(
            specs["get_cohort_projection"],
            GetCohortProjectionArgs,
            lambda args: economics_args.GetCohortProjectionArgs(
                business_id=args.business_id,
                product_id=args.product_id,
                platform=args.platform.value,
                observed=args.observed,
                age_days=args.age_days,
            ),
        ),
        _adapt(
            specs["get_platform_divergence"],
            GetPlatformDivergenceArgs,
            lambda args: economics_args.GetPlatformDivergenceArgs(
                business_id=args.business_id, platform_account_id=args.platform_account_id
            ),
        ),
    ]


def _adapt[ArgsT: ToolArgs](
    spec: ToolSpec,
    mcp_args_model: type[ArgsT],
    to_spec_args: Callable[[ArgsT], Any],
) -> ToolDefinition[ArgsT]:
    async def handler(args: ArgsT, _caller_scope: CallerScope) -> object:
        return await spec.handler(to_spec_args(args))

    return ToolDefinition(
        name=spec.name,
        description=spec.description,
        args_model=mcp_args_model,
        tool_class=ToolClass.PROPOSAL if spec.verb_kind == "proposal" else ToolClass.READ,
        handler=handler,
        business_id_of=_by_business_id,
    )


def _by_business_id(args: Any) -> str:  # noqa: ANN401 - extractor generico por posicion
    return str(args.business_id)
