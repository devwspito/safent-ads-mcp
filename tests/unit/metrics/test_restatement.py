"""`Restatement`: solo-anexable, guarda old/new por metrica corregida
(data-model.md §Restatement; NFR-7)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from safent_ads.metrics.domain.restatement import Restatement
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

_ENTITY = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id="c1")


def test_is_noop_when_values_are_equal() -> None:
    restatement = Restatement(
        entity_ref=_ENTITY,
        stat_date=date(2026, 9, 1),
        stat_hour=None,
        field_name="conversions.business_conversion",
        old_value=3,
        new_value=3,
        reason="reproceso sin cambios",
        recorded_at=datetime(2026, 9, 9, tzinfo=UTC),
    )

    assert restatement.is_noop is True


def test_is_not_noop_when_values_differ() -> None:
    restatement = Restatement(
        entity_ref=_ENTITY,
        stat_date=date(2026, 9, 1),
        stat_hour=None,
        field_name="conversions.business_conversion",
        old_value=3,
        new_value=5,
        reason="conversion tardia via CRM",
        recorded_at=datetime(2026, 9, 9, tzinfo=UTC),
    )

    assert restatement.is_noop is False
