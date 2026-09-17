"""004 tasks-2.md Carril R (R2 `get_crm_summary`, R8 `list_top_performing_ads`):
DTOs y puertos propios de las dos unicas lecturas que faltaban en la
auditoria de "lectura completa de la empresa" (historia 12, R1). Modulo
autonomo, mismo criterio de aislamiento que `search_terms_ports.py`: no
toca `mcp/application/ports.py` ni `read_model_ports.py` compartidos.

Puro: sin I/O, sin framework. `CrmSummary` es agregado sin PII (D-6,
threat-model.md §1 "CRM es PII agregada"): ningun campo de texto libre del
CRM, ningun identificador de cliente."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Protocol

__all__ = [
    "ChannelRevenue",
    "CrmSummary",
    "CrmSummaryReadPort",
    "CrmWindowPreset",
    "TopAdsLevel",
    "TopAdsMetric",
    "TopAdsWindowPreset",
    "TopPerformingAd",
    "TopPerformingAdMetrics",
    "TopPerformingAdsReadPort",
    "TopPerformingAdsResult",
    "crm_window_bounds",
    "top_ads_window_bounds",
]

# Nota fija (nunca texto libre) que sustituye cualquier cubo con menos de 5
# clientes contribuyendo (R2, minimo de agregacion k=5). El propio dato
# nunca sale -- ni siquiera un 0..4 exacto, la regla no distingue casos.
SUPPRESSED_BUCKET_NOTE = "cubo_con_menos_de_5_clientes"


class CrmWindowPreset(StrEnum):
    SEVEN_DAYS = "7D"
    THIRTY_DAYS = "30D"
    NINETY_DAYS = "90D"


_CRM_WINDOW_DAYS: dict[CrmWindowPreset, int] = {
    CrmWindowPreset.SEVEN_DAYS: 7,
    CrmWindowPreset.THIRTY_DAYS: 30,
    CrmWindowPreset.NINETY_DAYS: 90,
}


def crm_window_bounds(window: CrmWindowPreset, today: date) -> tuple[date, date]:
    """Ventana `[start, end]` inclusiva terminando hoy, tamano fijo por preset."""
    days = _CRM_WINDOW_DAYS[window]
    return today - timedelta(days=days - 1), today


@dataclass(frozen=True, slots=True)
class ChannelRevenue:
    """`revenue_minor`/`note` son mutuamente excluyentes: uno de los dos es
    siempre `None` (R2, minimo de agregacion k=5 por canal)."""

    channel: str
    revenue_minor: int | None
    note: str | None


@dataclass(frozen=True, slots=True)
class CrmSummary:
    business_id: str
    window_preset: CrmWindowPreset
    window_start: date
    window_end: date
    currency: str
    new_customers: int | None
    new_customers_note: str | None
    returning_customers: int | None
    returning_customers_note: str | None
    total_revenue_minor: int | None
    average_order_value_minor: int | None
    lifetime_value_estimate_minor: int | None
    revenue_note: str | None
    revenue_by_channel: tuple[ChannelRevenue, ...]


class CrmSummaryReadPort(Protocol):
    async def get_crm_summary(self, business_id: str, *, window: CrmWindowPreset) -> CrmSummary: ...


class TopAdsLevel(StrEnum):
    CAMPAIGN = "campaign"
    AD_SET = "ad_set"
    AD = "ad"


class TopAdsMetric(StrEnum):
    CONVERSIONS = "conversions"
    CPA = "cpa"
    ROAS = "roas"
    CTR = "ctr"
    SPEND = "spend"


class TopAdsWindowPreset(StrEnum):
    THIRTY_DAYS = "30D"
    SIXTY_DAYS = "60D"
    NINETY_DAYS = "90D"


_TOP_ADS_WINDOW_DAYS: dict[TopAdsWindowPreset, int] = {
    TopAdsWindowPreset.THIRTY_DAYS: 30,
    TopAdsWindowPreset.SIXTY_DAYS: 60,
    TopAdsWindowPreset.NINETY_DAYS: 90,
}


def top_ads_window_bounds(
    window: TopAdsWindowPreset, today: date
) -> tuple[date, date, date, date]:
    """`(current_start, current_end, previous_start, previous_end)`: la
    ventana anterior es contigua e igual de larga, para el `delta` de la
    historia 24 ("mejor resultado en 30-90 dias")."""
    days = _TOP_ADS_WINDOW_DAYS[window]
    current_end = today
    current_start = today - timedelta(days=days - 1)
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)
    return current_start, current_end, previous_start, previous_end


@dataclass(frozen=True, slots=True)
class TopPerformingAdMetrics:
    spend_minor: int
    conversions: int
    cpa_minor: int | None
    roas: float | None
    ctr: float | None


@dataclass(frozen=True, slots=True)
class TopPerformingAd:
    entity_ref: str
    name: str
    level: TopAdsLevel
    currency: str
    current: TopPerformingAdMetrics
    previous: TopPerformingAdMetrics
    delta_metric_pct: float | None


@dataclass(frozen=True, slots=True)
class TopPerformingAdsResult:
    business_id: str
    window_preset: TopAdsWindowPreset
    level: TopAdsLevel
    metric: TopAdsMetric
    ads: tuple[TopPerformingAd, ...]


class TopPerformingAdsReadPort(Protocol):
    async def list_top_performing_ads(
        self,
        business_id: str,
        *,
        window: TopAdsWindowPreset,
        level: TopAdsLevel,
        metric: TopAdsMetric,
        limit: int,
    ) -> TopPerformingAdsResult: ...
