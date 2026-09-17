"""`CompareAttributionWindows` (tool-surface.md §2.4, contracts/mcp-tools.md
P2 `compare_attribution_windows`): "Misma campana con 1/7/28 dias y CRM al
lado: donde miente la plataforma". Tres ventanas TRAILING (terminan en
`as_of`, longitudes 1/7/28 dias) de la MISMA entidad -- muestra como el
hueco se estrecha segun se ensancha la ventana (el efecto del rezago de
atribucion, profitability-engine.md §2), no el ajuste de ventana de
atribucion interno de cada plataforma (no observable con el dato que
ingerimos hoy -- ver Assumption en el modulo de presentacion)."""

from __future__ import annotations

from datetime import date, timedelta

from safent_ads.crm.application.ports import LeadAttributionRepository
from safent_ads.crm.domain.conversion_kind import ConversionKind as CrmConversionKind
from safent_ads.economics.application.dto import AttributionWindowRow
from safent_ads.economics.application.platform_conversions import sum_platform_conversions
from safent_ads.metrics.application.ports import MetricFactRepository
from safent_ads.shared.ids import BusinessId, EntityRef

_WINDOW_LENGTHS_DAYS = (1, 7, 28)


class CompareAttributionWindows:
    def __init__(
        self, lead_attributions: LeadAttributionRepository, metrics: MetricFactRepository
    ) -> None:
        self._lead_attributions = lead_attributions
        self._metrics = metrics

    async def execute(
        self, *, business_id: BusinessId, entity_ref: EntityRef, as_of: date
    ) -> list[AttributionWindowRow]:
        return [
            await self._row(business_id, entity_ref, days, as_of)
            for days in _WINDOW_LENGTHS_DAYS
        ]

    async def _row(
        self, business_id: BusinessId, entity_ref: EntityRef, days: int, as_of: date
    ) -> AttributionWindowRow:
        window_start = as_of - timedelta(days=days)
        crm_conversions = await self._lead_attributions.count_by_kind_in_window(
            business_id=business_id,
            conversion_kind=CrmConversionKind.BUSINESS_CONVERSION,
            window_start=window_start,
            window_end=as_of,
            entity_ref=str(entity_ref),
        )
        platform_conversions = await sum_platform_conversions(
            self._metrics, [entity_ref], window_start=window_start, window_end=as_of
        )
        gap_pct = (
            (platform_conversions - crm_conversions) / platform_conversions
            if platform_conversions
            else 0.0
        )
        return AttributionWindowRow(
            window_days=days,
            platform_conversions=platform_conversions,
            crm_conversions=crm_conversions,
            gap_pct=gap_pct,
        )
