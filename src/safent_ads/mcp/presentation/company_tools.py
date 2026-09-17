"""004 tasks-2.md R2/R8: `get_crm_summary` (historia 12, unica laguna real de
la auditoria de lectura completa) y `list_top_performing_ads` (historia 24,
pata (b) de la revision previa obligatoria). Modulo autonomo sobre
`mcp.application.company_read_ports`: declara sus propios `Args`, no toca
`args.py`/`handlers.py` compartidos (mismo criterio que
`search_terms_tools.py`)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import Field

from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.application.company_read_ports import (
    CrmSummary,
    CrmSummaryReadPort,
    CrmWindowPreset,
    TopAdsLevel,
    TopAdsMetric,
    TopAdsWindowPreset,
    TopPerformingAdsReadPort,
    TopPerformingAdsResult,
)
from safent_ads.mcp.presentation.args import BusinessId, ToolArgs
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition

__all__ = ["CompanyToolServices", "build_company_tool_definitions"]

type Handler[ArgsT, ReturnT] = Callable[[ArgsT, CallerScope], Awaitable[ReturnT]]

_MAX_TOP_ADS_LIMIT = 25
_DEFAULT_TOP_ADS_LIMIT = 10


@dataclass(frozen=True, slots=True)
class CompanyToolServices:
    crm_summary: CrmSummaryReadPort
    top_performing_ads: TopPerformingAdsReadPort


class GetCrmSummaryArgs(ToolArgs):
    business_id: BusinessId
    window: CrmWindowPreset = CrmWindowPreset.THIRTY_DAYS


class ListTopPerformingAdsArgs(ToolArgs):
    business_id: BusinessId
    window: TopAdsWindowPreset = TopAdsWindowPreset.THIRTY_DAYS
    level: TopAdsLevel = TopAdsLevel.CAMPAIGN
    metric: TopAdsMetric = TopAdsMetric.CONVERSIONS
    limit: int = Field(default=_DEFAULT_TOP_ADS_LIMIT, ge=1, le=_MAX_TOP_ADS_LIMIT)


def build_company_tool_definitions(services: CompanyToolServices) -> list[ToolDefinition[Any]]:
    return [
        ToolDefinition(
            name="get_crm_summary",
            description=(
                "Economia real del negocio en una ventana: clientes nuevos y que repiten, "
                "ingreso total, ticket medio, valor de vida estimado e ingreso por canal. "
                "Agregado, sin ningun cliente identificable: un cubo con menos de 5 clientes "
                "devuelve null y una nota, nunca el dato."
            ),
            args_model=GetCrmSummaryArgs,
            tool_class=ToolClass.READ,
            handler=_get_crm_summary(services.crm_summary),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="list_top_performing_ads",
            description=(
                "Anuncios propios con mejor resultado en 30-90 dias, con su cifra del periodo, "
                "el delta contra el periodo anterior y el entity_ref para encadenar con "
                "get_entity_metrics. Parte obligatoria de la revision previa antes de proponer "
                "una campana o un paquete."
            ),
            args_model=ListTopPerformingAdsArgs,
            tool_class=ToolClass.READ,
            handler=_list_top_performing_ads(services.top_performing_ads),
            business_id_of=_by_business_id,
        ),
    ]


def _by_business_id(args: Any) -> str:  # noqa: ANN401 - extractor generico por posicion
    return str(args.business_id)


def _get_crm_summary(port: CrmSummaryReadPort) -> Handler[GetCrmSummaryArgs, CrmSummary]:
    async def handler(args: GetCrmSummaryArgs, _caller_scope: CallerScope) -> CrmSummary:
        return await port.get_crm_summary(args.business_id, window=args.window)

    return handler


def _list_top_performing_ads(
    port: TopPerformingAdsReadPort,
) -> Handler[ListTopPerformingAdsArgs, TopPerformingAdsResult]:
    async def handler(
        args: ListTopPerformingAdsArgs, _caller_scope: CallerScope
    ) -> TopPerformingAdsResult:
        return await port.list_top_performing_ads(
            args.business_id,
            window=args.window,
            level=args.level,
            metric=args.metric,
            limit=args.limit,
        )

    return handler
