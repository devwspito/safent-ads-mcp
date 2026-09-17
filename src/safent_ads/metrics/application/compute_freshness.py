"""`ComputeFreshness` (plan.md §5, tasks.md T031, NFR-1: frescura <= 60 min en
horario activo, siempre visible).

Toma el `ingested_at` mas reciente entre las entidades de una cuenta+nivel y
deriva `Freshness.is_stale` con el reloj inyectado, nunca `datetime.now`."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from safent_ads.metrics.application.errors import NoMetricsIngestedError
from safent_ads.metrics.application.ports import MetricFactRepository
from safent_ads.metrics.domain.freshness import Freshness
from safent_ads.shared.ids import EntityLevel, EntityRef


@dataclass(frozen=True, kw_only=True, slots=True)
class ComputeFreshnessRequest:
    platform_account_ref: str
    entity_level: EntityLevel
    entity_refs: Sequence[EntityRef]
    now: datetime


class ComputeFreshness:
    def __init__(self, repository: MetricFactRepository) -> None:
        self._repository = repository

    async def execute(self, request: ComputeFreshnessRequest) -> Freshness:
        latest = await self._latest_ingested_at(request.entity_refs)
        return Freshness(
            platform_account_ref=request.platform_account_ref,
            entity_level=request.entity_level,
            last_ingested_at=latest,
        )

    async def _latest_ingested_at(self, entity_refs: Sequence[EntityRef]) -> datetime:
        timestamps = [
            timestamp
            for entity_ref in entity_refs
            if (timestamp := await self._repository.latest_ingested_at(entity_ref=entity_ref))
            is not None
        ]
        if not timestamps:
            raise NoMetricsIngestedError("sin MetricFact ingerido para las entidades pedidas")
        return max(timestamps)
