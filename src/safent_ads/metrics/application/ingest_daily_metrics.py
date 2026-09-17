"""`IngestDailyMetrics` (plan.md §5, tasks.md T031): UPSERT de hechos diarios.

Idempotente por construccion: delega en `MetricFactRepository.upsert_many`,
cuya clave natural es `(entity_ref, stat_date, stat_hour=None)` (FR-5, NFR-6)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from safent_ads.metrics.application.errors import MixedBatchStatHourError
from safent_ads.metrics.application.ports import MetricFactRepository
from safent_ads.metrics.domain.metric_fact import MetricFact


@dataclass(frozen=True, kw_only=True, slots=True)
class IngestDailyMetricsRequest:
    facts: Sequence[MetricFact]


class IngestDailyMetrics:
    def __init__(self, repository: MetricFactRepository) -> None:
        self._repository = repository

    async def execute(self, request: IngestDailyMetricsRequest) -> None:
        self._reject_hourly_facts(request.facts)
        await self._repository.upsert_many(request.facts)

    @staticmethod
    def _reject_hourly_facts(facts: Sequence[MetricFact]) -> None:
        if any(fact.is_hourly for fact in facts):
            raise MixedBatchStatHourError("un lote diario no admite facts con stat_hour")
