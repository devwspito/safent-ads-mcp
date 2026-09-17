"""Adapta las 4 `ToolSpec` P1 restantes de `optimization` (profitability-
engine.md §8: `get_marginal_roas`, `diagnose_entity`, `simulate_spend_change`,
`propose_reallocation_plan`; las 5 de economia unitaria viven en
`economics_tools.py`) al `ToolRegistry` real -- mismo hueco que
`checklists/final-review.md` B-1 describe para `economics_tools.py`.

Mismo criterio que ese modulo: `optimization` no importa `mcp` (plan.md
§4), asi que sus `ToolSpec` declaran argumentos sobre
`optimization.presentation.args.OptimizationToolArgs`, que no hereda de
`mcp.presentation.args.ToolArgs`. Este modulo declara su propia copia de
los argumentos (SI hereda de `ToolArgs`) y reconstruye la instancia del
`args_model` original antes de llamar a `spec.handler` -- la logica de
negocio y el mapeo de errores tipados siguen intactos en
`optimization/presentation/mcp_tools.py`.

`propose_reallocation_plan` es `ToolClass.PROPOSAL` (`spec.verb_kind ==
"proposal"`): crea dos `PropuestaDeAccion` ligadas via
`ReallocationProposalPort`, nunca escribe en plataforma (la unica
escritura sigue siendo `apply_defensive_action`)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from pydantic import Field

from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.presentation.args import BusinessId, EntityRefStr, ToolArgs
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition
from safent_ads.optimization.application.query_service import OptimizationQueryService
from safent_ads.optimization.presentation import args as optimization_args
from safent_ads.optimization.presentation.mcp_tools import build_optimization_tool_specs
from safent_ads.optimization.presentation.tool_spec import ToolSpec

__all__ = ["build_optimization_tool_definitions"]

type Handler[ArgsT, ReturnT] = Callable[[ArgsT, CallerScope], Awaitable[ReturnT]]

_UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
ProductIdStr = Annotated[str, Field(pattern=_UUID_PATTERN)]


class GetMarginalRoasArgs(ToolArgs):
    business_id: BusinessId
    entity_ref: EntityRefStr


class DiagnoseEntityArgs(ToolArgs):
    business_id: BusinessId
    entity_ref: EntityRefStr


class SimulateSpendChangeArgs(ToolArgs):
    business_id: BusinessId
    entity_ref: EntityRefStr
    product_id: ProductIdStr
    current_daily_spend_amount: str
    spend_multiplier: float = Field(gt=0)


class ProposeReallocationPlanArgs(ToolArgs):
    business_id: BusinessId


def build_optimization_tool_definitions(
    service: OptimizationQueryService,
) -> list[ToolDefinition[Any]]:
    specs = {spec.name: spec for spec in build_optimization_tool_specs(service)}
    return [
        _adapt(
            specs["get_marginal_roas"],
            GetMarginalRoasArgs,
            lambda args: optimization_args.GetMarginalRoasArgs(
                business_id=args.business_id, entity_ref=args.entity_ref
            ),
        ),
        _adapt(
            specs["diagnose_entity"],
            DiagnoseEntityArgs,
            lambda args: optimization_args.DiagnoseEntityArgs(
                business_id=args.business_id, entity_ref=args.entity_ref
            ),
        ),
        _adapt(
            specs["simulate_spend_change"],
            SimulateSpendChangeArgs,
            lambda args: optimization_args.SimulateSpendChangeArgs(
                business_id=args.business_id,
                entity_ref=args.entity_ref,
                product_id=args.product_id,
                current_daily_spend_amount=args.current_daily_spend_amount,
                spend_multiplier=args.spend_multiplier,
            ),
        ),
        _adapt(
            specs["propose_reallocation_plan"],
            ProposeReallocationPlanArgs,
            lambda args: optimization_args.ProposeReallocationPlanArgs(
                business_id=args.business_id
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
