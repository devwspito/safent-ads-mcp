"""Contrato de `MetricFactRepository` y `RestatementRepository`."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from safent_ads.metrics.domain.conversion_kind import ConversionKind
from safent_ads.metrics.domain.restatement import Restatement
from tests.contracts.metrics.conftest import INGESTED_AT, MetricsFixture, build_fact
from tests.contracts.sql_fixtures import campaign_ref


async def test_upsert_is_idempotent(metrics: MetricsFixture) -> None:
    entity_ref = campaign_ref("idempotente")
    await metrics.given_entity(entity_ref)
    fact = build_fact(entity_ref)

    await metrics.facts.upsert_many([fact])
    await metrics.facts.upsert_many([fact])

    window = await metrics.facts.find_in_window(
        entity_ref=entity_ref, start_date=date(2026, 3, 1), end_date=date(2026, 3, 31)
    )
    assert len(window) == 1
    assert window[0] == fact


@pytest.mark.parametrize("hour", [None, 9])
async def test_delayed_retry_cannot_replace_a_newer_observation(
    metrics: MetricsFixture,
    hour: int | None,
) -> None:
    ref = campaign_ref(f"delayed-{hour}")
    await metrics.given_entity(ref)
    newest = build_fact(ref, stat_hour=hour, spend_minor=400)
    oldest = replace(newest, spend_minor=100, ingested_at=newest.ingested_at - timedelta(hours=1))
    await metrics.facts.upsert_many([newest, oldest])
    assert (
        await metrics.facts.find_by_natural_key(
            entity_ref=ref, stat_date=newest.stat_date, stat_hour=hour
        )
        == newest
    )


async def test_upsert_replaces_the_previous_counters(metrics: MetricsFixture) -> None:
    entity_ref = campaign_ref("corregido")
    await metrics.given_entity(entity_ref)
    await metrics.facts.upsert_many([build_fact(entity_ref, spend_minor=7800)])

    corrected = build_fact(
        entity_ref,
        spend_minor=9100,
        conversions={ConversionKind.LEAD: 14, ConversionKind.BUSINESS_CONVERSION: 2},
        revision=2,
    )
    await metrics.facts.upsert_many([corrected])

    stored = await metrics.facts.find_by_natural_key(
        entity_ref=entity_ref, stat_date=corrected.stat_date, stat_hour=None
    )
    assert stored == corrected


async def test_daily_and_hourly_are_different_facts(metrics: MetricsFixture) -> None:
    entity_ref = campaign_ref("horario")
    await metrics.given_entity(entity_ref)
    daily = build_fact(entity_ref)
    hourly = build_fact(entity_ref, stat_hour=9, spend_minor=400)

    await metrics.facts.upsert_many([daily, hourly])

    assert (
        await metrics.facts.find_by_natural_key(
            entity_ref=entity_ref, stat_date=daily.stat_date, stat_hour=None
        )
        == daily
    )
    assert (
        await metrics.facts.find_by_natural_key(
            entity_ref=entity_ref, stat_date=hourly.stat_date, stat_hour=9
        )
        == hourly
    )


async def test_unknown_natural_key_is_none(metrics: MetricsFixture) -> None:
    entity_ref = campaign_ref("vacio")
    await metrics.given_entity(entity_ref)

    assert (
        await metrics.facts.find_by_natural_key(
            entity_ref=entity_ref, stat_date=date(2026, 3, 14), stat_hour=None
        )
        is None
    )


async def test_window_excludes_days_outside_the_range(metrics: MetricsFixture) -> None:
    entity_ref = campaign_ref("ventana")
    await metrics.given_entity(entity_ref)
    await metrics.facts.upsert_many(
        [build_fact(entity_ref, day=10), build_fact(entity_ref, day=20)]
    )

    window = await metrics.facts.find_in_window(
        entity_ref=entity_ref, start_date=date(2026, 3, 15), end_date=date(2026, 3, 25)
    )

    assert [fact.stat_date for fact in window] == [date(2026, 3, 20)]


async def test_impression_share_survives_the_round_trip(metrics: MetricsFixture) -> None:
    entity_ref = campaign_ref("cuota")
    await metrics.given_entity(entity_ref)
    fact = build_fact(entity_ref, search_lost_is_budget_pct=23.0)

    await metrics.facts.upsert_many([fact])
    stored = await metrics.facts.find_by_natural_key(
        entity_ref=entity_ref, stat_date=fact.stat_date, stat_hour=None
    )

    assert stored is not None
    assert stored.search_lost_is_budget_pct == 23.0


async def test_latest_ingested_at_is_the_most_recent(metrics: MetricsFixture) -> None:
    entity_ref = campaign_ref("frescura")
    await metrics.given_entity(entity_ref)
    later = datetime(2026, 9, 9, 18, 0, tzinfo=UTC)
    first = build_fact(entity_ref, day=10)
    second = build_fact(entity_ref, day=11)
    await metrics.facts.upsert_many([first])
    await metrics.facts.upsert_many([replace(second, ingested_at=later)])

    assert await metrics.facts.latest_ingested_at(entity_ref=entity_ref) == later


async def test_latest_ingested_at_without_facts_is_none(metrics: MetricsFixture) -> None:
    entity_ref = campaign_ref("sin-datos")
    await metrics.given_entity(entity_ref)

    assert await metrics.facts.latest_ingested_at(entity_ref=entity_ref) is None


async def test_restatements_are_appended_in_order(metrics: MetricsFixture) -> None:
    entity_ref = campaign_ref("correcciones")
    await metrics.given_entity(entity_ref)
    first = Restatement(
        entity_ref=entity_ref,
        stat_date=date(2026, 3, 14),
        stat_hour=None,
        field_name="conversions.lead",
        old_value=12,
        new_value=14,
        reason="conversion tardia",
        recorded_at=INGESTED_AT,
    )
    second = Restatement(
        entity_ref=entity_ref,
        stat_date=date(2026, 3, 14),
        stat_hour=None,
        field_name="spend_minor",
        old_value=7800,
        new_value=9100,
        reason="reproceso de plataforma",
        recorded_at=INGESTED_AT,
    )

    await metrics.restatements.record(first)
    await metrics.restatements.record(second)

    assert list(await metrics.restatements.list_for_entity(entity_ref=entity_ref)) == [
        first,
        second,
    ]
