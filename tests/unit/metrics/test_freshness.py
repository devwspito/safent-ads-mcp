"""`Freshness`: `is_stale` derivado de `lag_minutes > threshold` (data-model.md
§Freshness; NFR-1: frescura <= 60 min en horario activo)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from safent_ads.metrics.domain.freshness import Freshness
from safent_ads.shared.ids import EntityLevel

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def test_lag_minutes_computed_from_now() -> None:
    freshness = Freshness(
        platform_account_ref="meta:123",
        entity_level=EntityLevel.CAMPAIGN,
        last_ingested_at=_NOW - timedelta(minutes=45),
    )

    assert freshness.lag_minutes(now=_NOW) == 45


def test_is_stale_true_over_threshold() -> None:
    freshness = Freshness(
        platform_account_ref="meta:123",
        entity_level=EntityLevel.CAMPAIGN,
        last_ingested_at=_NOW - timedelta(minutes=61),
    )

    assert freshness.is_stale(now=_NOW, threshold_minutes=60) is True


def test_is_stale_false_at_threshold_boundary() -> None:
    freshness = Freshness(
        platform_account_ref="meta:123",
        entity_level=EntityLevel.CAMPAIGN,
        last_ingested_at=_NOW - timedelta(minutes=60),
    )

    assert freshness.is_stale(now=_NOW, threshold_minutes=60) is False
