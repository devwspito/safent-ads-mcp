"""`IngestHourlyMetrics` (plan.md §5, tasks.md T031): UPSERT de hechos horarios."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from safent_ads.metrics.application.errors import MixedBatchStatHourError
from safent_ads.metrics.application.ports import MetricFactRepository
from safent_ads.metrics.domain.metric_fact import MetricFact


@dataclass(frozen=True, kw_only=True, slots=True)
class IngestHourlyMetricsRequest:
    facts: Sequence[MetricFact]


class IngestHourlyMetrics:
    def __init__(self, repository: MetricFactRepository) -> None:
        self._repository = repository

    async def execute(self, request: IngestHourlyMetricsRequest) -> None:
        self._require_hourly_facts(request.facts)
        await self._repository.upsert_many(request.facts)

    @staticmethod
    def _require_hourly_facts(facts: Sequence[MetricFact]) -> None:
        if any(not fact.is_hourly for fact in facts):
            raise MixedBatchStatHourError("un lote horario exige stat_hour en todo fact")
