"""`DetectAnomalies` (plan.md §5, tasks.md T037): `same_weekday_z` sobre la
serie diaria -> `Anomaly`. Solo notifica, nunca produce accion directa
(data-model.md §Anomaly)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from safent_ads.shared.ids import EntityRef
from safent_ads.signals.application.ports import AnomalyRepository, DailySpendSeriesRepository
from safent_ads.signals.domain.anomaly import Anomaly, AnomalyMethod, classify_z_severity
from safent_ads.signals.domain.anomaly import same_weekday_z as compute_same_weekday_z

_DEFAULT_LOOKBACK_WEEKS = 8


@dataclass(frozen=True, kw_only=True, slots=True)
class DetectAnomaliesRequest:
    entity_ref: EntityRef
    as_of: datetime
    lookback_weeks: int = _DEFAULT_LOOKBACK_WEEKS


class DetectAnomalies:
    def __init__(self, series: DailySpendSeriesRepository, anomalies: AnomalyRepository) -> None:
        self._series = series
        self._anomalies = anomalies

    async def execute(self, request: DetectAnomaliesRequest) -> Anomaly:
        same_weekday_values = await self._series.fetch_same_weekday_series(
            entity_ref=request.entity_ref, as_of=request.as_of.date(), weeks=request.lookback_weeks
        )
        today_value = await self._series.fetch_today_value(
            entity_ref=request.entity_ref, as_of=request.as_of.date()
        )
        z = compute_same_weekday_z(today_value, same_weekday_values)
        anomaly = Anomaly(
            entity_ref=request.entity_ref,
            method=AnomalyMethod.WEEKDAY_Z,
            score=z,
            severity=classify_z_severity(z),
            detected_at=request.as_of,
        )
        await self._anomalies.save(anomaly)
        return anomaly
