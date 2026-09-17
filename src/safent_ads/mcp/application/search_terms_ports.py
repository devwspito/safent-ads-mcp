"""DTOs y puertos propios de `list_search_terms`/`get_budget_envelope`
(tool-surface.md §2.1 P2). Modulo autonomo, no toca `mcp/application/
ports.py` ni `read_model_ports.py` compartidos -- mismo criterio de
aislamiento que `mcp.presentation.experiment_tools` (menos superficie
compartida entre carriles concurrentes). Pura: sin I/O, sin framework."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from safent_ads.mcp.application.dto import Window
from safent_ads.shared.read_models.dto import Money

__all__ = [
    "BudgetEnvelope",
    "BudgetEnvelopeReadPort",
    "SearchTermReadPort",
    "SearchTermSummary",
    "SearchTermsResult",
]


@dataclass(frozen=True, slots=True)
class SearchTermSummary:
    term: str
    cost: Money
    conversions: int
    matched_keyword: str | None
    campaign_ref: str
    ad_group_ref: str


@dataclass(frozen=True, slots=True)
class SearchTermsResult:
    """`is_supported=False` (Meta) es una respuesta tipada valida, nunca un
    error: `terms` viene vacia y `reason` explica por que (contracts/
    mcp-tools.md: `run_gaql`/`search_term_view` son especificos de Google)."""

    account_ref: str
    is_supported: bool
    reason: str | None
    terms: list[SearchTermSummary]


class SearchTermReadPort(Protocol):
    async def list_search_terms(
        self, business_id: str, account_ref: str, *, window: Window
    ) -> SearchTermsResult: ...


@dataclass(frozen=True, slots=True)
class BudgetEnvelope:
    """`reason=None` cuando se conocen tope y gasto. `reason` poblado
    (`"no_platform_account"`/`"no_caps_entry"`) cuando el tope mensual no
    se puede derivar: `monthly_cap_minor`/`headroom_minor` quedan a `None`
    en vez de un numero inventado. `spent_month_to_date_minor`/
    `projected_month_end_minor` son hechos medidos/proyectados a partir del
    gasto real -- se reportan siempre que exista una cuenta, con o sin
    tope configurado (no dependen de `monthly_cap_minor`)."""

    business_id: str
    monthly_cap_minor: int | None
    spent_month_to_date_minor: int | None
    headroom_minor: int | None
    projected_month_end_minor: int | None
    currency: str | None
    as_of: date
    reason: str | None


class BudgetEnvelopeReadPort(Protocol):
    async def get_budget_envelope(self, business_id: str) -> BudgetEnvelope: ...
