"""`ComputePlatformDivergence` (T156, profitability-engine.md §2): `delta_hat`
sobre las 8 semanas cerradas mas recientes -- CRM (`crm.LeadAttribution`,
conteo de `business_conversion`) contra plataforma (`metrics.MetricFact`,
suma de `conversions_business_conversion`) para las entidades de una cuenta.

`entity_refs` llega ya resuelto por quien orquesta (`orchestration`, que si
puede leer `accounts`): este contexto no depende de `accounts` en el grafo
(profitability-engine.md: 'economics sobre catalog/crm/metrics')."""

from __future__ import annotations

from collections.abc import Sequence

from safent_ads.crm.application.ports import LeadAttributionRepository
from safent_ads.crm.domain.conversion_kind import ConversionKind as CrmConversionKind
from safent_ads.economics.application.platform_conversions import sum_platform_conversions
from safent_ads.economics.application.ports import PlatformDivergenceRepository
from safent_ads.economics.domain.platform_divergence import (
    DEFAULT_SHRINKAGE_M,
    PlatformDivergence,
    closed_eight_week_window,
)
from safent_ads.metrics.application.ports import MetricFactRepository
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId, EntityRef


class ComputePlatformDivergence:
    def __init__(
        self,
        lead_attributions: LeadAttributionRepository,
        metrics: MetricFactRepository,
        divergences: PlatformDivergenceRepository,
        clock: Clock,
        *,
        shrinkage_m: int = DEFAULT_SHRINKAGE_M,
    ) -> None:
        self._lead_attributions = lead_attributions
        self._metrics = metrics
        self._divergences = divergences
        self._clock = clock
        self._shrinkage_m = shrinkage_m

    async def execute(
        self,
        *,
        business_id: BusinessId,
        platform_account_id: str,
        entity_refs: Sequence[EntityRef],
    ) -> PlatformDivergence | None:
        if not entity_refs:
            return None
        window_start, window_end = closed_eight_week_window(self._clock.now().date())
        crm_conversions = await self._lead_attributions.count_by_kind_in_window(
            business_id=business_id,
            conversion_kind=CrmConversionKind.BUSINESS_CONVERSION,
            window_start=window_start,
            window_end=window_end,
        )
        platform_conversions = await sum_platform_conversions(
            self._metrics, entity_refs, window_start=window_start, window_end=window_end
        )
        divergence = PlatformDivergence.compute(
            crm_conversions=crm_conversions,
            platform_conversions=platform_conversions,
            shrinkage_m=self._shrinkage_m,
            window_start=window_start,
            window_end=window_end,
        )
        await self._divergences.save(
            business_id=business_id, platform_account_id=platform_account_id, divergence=divergence
        )
        return divergence
