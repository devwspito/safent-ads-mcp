"""`GetCrmReconciliation` (contracts/mcp-tools.md P2 `get_crm_reconciliation`,
tool-surface.md §2.4): plataforma vs CRM sobre una ventana, con el rezago
declarado (FR-3). Nunca corrige gasto -- solo traduce (profitability-
engine.md §2).

`customer_bridge_healthy` (spec 027, T017): la MISMA salud que lee
`SqlMeasurementFreezeGate` para congelar BUY, expuesta en el informe de
reconciliacion en vez de un mecanismo aparte -- `crm_bridge_health` puerto
opcional porque este caso de uso corre en negocios que todavia no tienen
un puente CRM configurado (`None`, nunca `True`/`False` fabricado)."""

from __future__ import annotations

from datetime import date

from safent_ads.crm.application.customer_ports import CrmBridgeHealthRepository
from safent_ads.crm.application.ports import LeadAttributionRepository
from safent_ads.crm.domain.conversion_kind import ConversionKind as CrmConversionKind
from safent_ads.economics.application.dto import CrmReconciliationView
from safent_ads.economics.application.platform_conversions import sum_platform_conversions
from safent_ads.metrics.application.ports import MetricFactRepository
from safent_ads.shared.ids import BusinessId


class GetCrmReconciliation:
    def __init__(
        self,
        lead_attributions: LeadAttributionRepository,
        metrics: MetricFactRepository,
        crm_bridge_health: CrmBridgeHealthRepository | None = None,
    ) -> None:
        self._lead_attributions = lead_attributions
        self._metrics = metrics
        self._crm_bridge_health = crm_bridge_health

    async def execute(
        self, *, business_id: BusinessId, window_start: date, window_end: date, lag_days: int = 0
    ) -> CrmReconciliationView:
        entity_refs = await self._lead_attributions.list_distinct_entity_refs_in_window(
            business_id=business_id, window_start=window_start, window_end=window_end
        )
        crm_conversions = await self._lead_attributions.count_by_kind_in_window(
            business_id=business_id,
            conversion_kind=CrmConversionKind.BUSINESS_CONVERSION,
            window_start=window_start,
            window_end=window_end,
        )
        platform_conversions = await sum_platform_conversions(
            self._metrics, entity_refs, window_start=window_start, window_end=window_end
        )
        gap_pct = (
            (platform_conversions - crm_conversions) / platform_conversions
            if platform_conversions
            else 0.0
        )
        return CrmReconciliationView(
            platform_conversions=platform_conversions,
            crm_conversions=crm_conversions,
            lag_days=lag_days,
            gap_pct=gap_pct,
            customer_bridge_healthy=await self._customer_bridge_healthy(business_id),
        )

    async def _customer_bridge_healthy(self, business_id: BusinessId) -> bool | None:
        if self._crm_bridge_health is None:
            return None
        health = await self._crm_bridge_health.get_for_business(business_id=business_id)
        return None if health is None else health.has_recent_events_24h
