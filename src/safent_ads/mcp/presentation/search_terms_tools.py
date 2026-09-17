"""Las 2 herramientas MCP P2 de `tool-surface.md` §2.1 que hoy dejan a
`campaign-triage` en un callejon sin salida: `list_search_terms`,
`get_budget_envelope`.

Modulo autonomo (mismo criterio de aislamiento que
`mcp.presentation.experiment_tools`): declara sus propios `Args` sobre
`mcp.presentation.args.ToolArgs` (`extra=forbid`, sin URLs libres,
`business_id` obligatorio -- regla 4 del contrato) y sus propios handlers,
sobre los puertos propios de `mcp.application.search_terms_ports`.
`catalog.py` la engancha con una linea (`build_search_terms_tool_definitions`),
sin tocar `handlers.py`/`read_model_ports.py`/`ports.py` compartidos.

Las dos son lectura pura (`ToolClass.READ`): ninguna persiste nada ni
crea una `PropuestaDeAccion` (regla 3 del contrato)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import model_validator

from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.application.dto import Window, WindowPreset
from safent_ads.mcp.application.search_terms_ports import (
    BudgetEnvelope,
    BudgetEnvelopeReadPort,
    SearchTermReadPort,
    SearchTermsResult,
)
from safent_ads.mcp.presentation.args import BusinessId as BusinessIdStr
from safent_ads.mcp.presentation.args import OpaqueId, ToolArgs
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition

__all__ = ["SearchTermsToolServices", "build_search_terms_tool_definitions"]

type Handler[ArgsT, ReturnT] = Callable[[ArgsT, CallerScope], Awaitable[ReturnT]]

# tool-surface.md P2 ("windows (7/14/30 dias)"): a diferencia de
# `GetEntityMetricsArgs`/`WindowArgs`, esta herramienta cierra el abanico a
# solo estos tres presets -- ni "hoy"/"3D"/"MTD" ni rango libre `from`/`to`.
_ALLOWED_SEARCH_TERMS_WINDOWS = frozenset(
    {WindowPreset.SEVEN_DAYS, WindowPreset.FOURTEEN_DAYS, WindowPreset.THIRTY_DAYS}
)


@dataclass(frozen=True, slots=True)
class SearchTermsToolServices:
    search_terms: SearchTermReadPort
    budget_envelope: BudgetEnvelopeReadPort


class ListSearchTermsArgs(ToolArgs):
    business_id: BusinessIdStr
    account_ref: OpaqueId
    window: WindowPreset

    @model_validator(mode="after")
    def _window_within_allowed_range(self) -> ListSearchTermsArgs:
        if self.window not in _ALLOWED_SEARCH_TERMS_WINDOWS:
            raise ValueError("list_search_terms: window debe ser 7D, 14D o 30D")
        return self


class GetBudgetEnvelopeArgs(ToolArgs):
    business_id: BusinessIdStr


def build_search_terms_tool_definitions(
    services: SearchTermsToolServices,
) -> list[ToolDefinition[Any]]:
    return [
        ToolDefinition(
            name="list_search_terms",
            description=(
                "Terminos de busqueda reales que dispararon un anuncio de "
                "Google Ads (fuera de campana en Meta: no soportado)."
            ),
            args_model=ListSearchTermsArgs,
            tool_class=ToolClass.READ,
            handler=_list_search_terms(services.search_terms),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="get_budget_envelope",
            description=(
                "Techo mensual y margen restante de un negocio: gasto a la "
                "fecha, margen y proyeccion a fin de mes."
            ),
            args_model=GetBudgetEnvelopeArgs,
            tool_class=ToolClass.READ,
            handler=_get_budget_envelope(services.budget_envelope),
            business_id_of=_by_business_id,
        ),
    ]


def _by_business_id(args: Any) -> str:  # noqa: ANN401 - extractor generico por posicion
    return str(args.business_id)


def _list_search_terms(
    port: SearchTermReadPort,
) -> Handler[ListSearchTermsArgs, SearchTermsResult]:
    async def handler(args: ListSearchTermsArgs, _caller_scope: CallerScope) -> SearchTermsResult:
        window = Window(preset=args.window, lag_days=0, date_from=None, date_to=None)
        return await port.list_search_terms(args.business_id, args.account_ref, window=window)

    return handler


def _get_budget_envelope(
    port: BudgetEnvelopeReadPort,
) -> Handler[GetBudgetEnvelopeArgs, BudgetEnvelope]:
    async def handler(args: GetBudgetEnvelopeArgs, _caller_scope: CallerScope) -> BudgetEnvelope:
        return await port.get_budget_envelope(args.business_id)

    return handler
