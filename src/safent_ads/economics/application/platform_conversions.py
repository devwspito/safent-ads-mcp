"""`sum_platform_conversions`: suma `conversions_business_conversion` de
`metrics.MetricFact` sobre un conjunto de entidades y una ventana
`[window_start, window_end)` -- ayudante compartido por
`ComputePlatformDivergence`, `GetCrmReconciliation` y
`CompareAttributionWindows` para no repetir el ajuste de borde (`metrics.
MetricFactRepository.find_in_window` es inclusiva en `end_date`; el
`window_end` de estos casos de uso es exclusivo, mismo criterio que
`crm.LeadAttributionRepository.count_by_kind_in_window`)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta

from safent_ads.metrics.application.ports import MetricFactRepository
from safent_ads.metrics.domain.conversion_kind import ConversionKind as MetricsConversionKind
from safent_ads.shared.ids import EntityRef


async def sum_platform_conversions(
    metrics: MetricFactRepository,
    entity_refs: Iterable[EntityRef | str],
    *,
    window_start: date,
    window_end: date,
) -> int:
    last_closed_day = window_end - timedelta(days=1)
    total = 0
    for entity_ref in entity_refs:
        resolved = entity_ref if isinstance(entity_ref, EntityRef) else EntityRef.parse(entity_ref)
        facts = await metrics.find_in_window(
            entity_ref=resolved, start_date=window_start, end_date=last_closed_day
        )
        total += sum(
            fact.conversions_of(MetricsConversionKind.BUSINESS_CONVERSION) for fact in facts
        )
    return total
