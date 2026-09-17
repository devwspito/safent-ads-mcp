"""`DetectAnomalies`: `same_weekday_z` -> `Anomaly` persistida, nunca accion
directa (data-model.md §Anomaly)."""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode
from safent_ads.signals.application.detect_anomalies import (
    DetectAnomalies,
    DetectAnomaliesRequest,
)
from safent_ads.signals.domain.anomaly import AnomalyMethod, AnomalySeverity
from safent_ads.signals.testing.in_memory_anomaly_repository import (
    InMemoryAnomalyRepository,
    InMemoryDailySpendSeriesRepository,
)

_ENTITY = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id="c1")
_AS_OF = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)


async def test_detects_a_paging_severity_spike() -> None:
    series = InMemoryDailySpendSeriesRepository(same_weekday_series=[100.0] * 8, today_value=250.0)
    anomalies = InMemoryAnomalyRepository()
    use_case = DetectAnomalies(series, anomalies)

    anomaly = await use_case.execute(DetectAnomaliesRequest(entity_ref=_ENTITY, as_of=_AS_OF))

    assert anomaly.method == AnomalyMethod.WEEKDAY_Z
    assert anomaly.severity == AnomalySeverity.PAGE
    assert anomalies.saved == [anomaly]


async def test_no_anomaly_when_within_normal_band() -> None:
    series = InMemoryDailySpendSeriesRepository(
        same_weekday_series=[100.0, 105.0, 95.0, 102.0, 98.0, 101.0, 99.0, 100.0],
        today_value=100.0,
    )
    anomalies = InMemoryAnomalyRepository()
    use_case = DetectAnomalies(series, anomalies)

    anomaly = await use_case.execute(DetectAnomaliesRequest(entity_ref=_ENTITY, as_of=_AS_OF))

    assert anomaly.severity == AnomalySeverity.NONE
