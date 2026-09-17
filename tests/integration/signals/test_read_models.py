"""`SqlMetricWindowRepository` y `SqlDailySpendSeriesRepository` contra
Postgres real.

Estos dos no llevan banco de contrato de doble comparado: los dobles en
memoria devuelven ventanas precargadas, asi que no hay comportamiento comun
que contrastar. Lo unico que hay que demostrar vive en SQL — que la ventana
suma lo que debe, que respeta el borde del intervalo, que no mezcla
entidades y que la serie del mismo dia de la semana sale ordenada.

Los hechos se escriben con `SqlMetricFactRepository` a proposito: la capa
anticorrupcion solo vale si lee en las mismas unidades en que el otro
contexto escribe (`metrics_daily.spend` guarda unidades menores)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from types import MappingProxyType

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.metrics.domain.conversion_kind import ConversionKind
from safent_ads.metrics.domain.metric_fact import MetricFact
from safent_ads.metrics.infrastructure.sql_repositories import SqlMetricFactRepository
from safent_ads.shared.ids import EntityRef
from safent_ads.signals.domain.window_span import WindowSpan
from safent_ads.signals.infrastructure.sql_repositories import (
    SqlDailySpendSeriesRepository,
    SqlMetricWindowRepository,
)
from tests.conftest import rolled_back_session
from tests.contracts.sql_fixtures import campaign_ref, seed_entity

pytestmark = pytest.mark.integration

AS_OF = datetime(2026, 3, 14, 23, 30, tzinfo=UTC)
INGESTED_AT = datetime(2026, 3, 15, 1, 0, tzinfo=UTC)


@pytest.fixture
async def session(database_url: str) -> AsyncIterator[AsyncSession]:
    async with rolled_back_session(database_url) as open_session:
        yield open_session


def build_fact(
    entity_ref: EntityRef,
    *,
    stat_date: date,
    spend_minor: int = 1000,
    impressions: int = 100,
    clicks: int = 10,
    reach: int = 90,
    conversions: int = 1,
    conversion_value_minor: int = 5000,
    search_lost_is_budget_pct: float | None = None,
) -> MetricFact:
    return MetricFact(
        entity_ref=entity_ref,
        stat_date=stat_date,
        account_timezone="Europe/Madrid",
        currency="EUR",
        spend_minor=spend_minor,
        impressions=impressions,
        clicks=clicks,
        reach=reach,
        conversions=MappingProxyType({ConversionKind.LEAD: conversions}),
        conversion_value_minor=conversion_value_minor,
        video_views_3s=30,
        video_views_75pct=9,
        search_lost_is_budget_pct=search_lost_is_budget_pct,
        ingested_at=INGESTED_AT,
    )


async def given_facts(session: AsyncSession, *facts: MetricFact) -> None:
    await SqlMetricFactRepository(session).upsert_many(facts)


async def test_window_sums_every_day_inside_the_span(session: AsyncSession) -> None:
    entity_ref = campaign_ref("win-suma")
    await seed_entity(session, entity_ref)
    await given_facts(
        session,
        *[
            build_fact(entity_ref, stat_date=date(2026, 3, day), spend_minor=1000, clicks=10)
            for day in range(8, 15)
        ],
    )

    window = await SqlMetricWindowRepository(session).fetch_window(
        entity_ref=entity_ref, span=WindowSpan.D7, as_of=AS_OF
    )

    assert window.spend_minor == 7000
    assert window.clicks == 70
    assert window.conversions == 7
    assert window.conversion_value_minor == 35000
    assert window.span is WindowSpan.D7


async def test_window_leaves_out_the_day_before_the_span(session: AsyncSession) -> None:
    entity_ref = campaign_ref("win-borde")
    await seed_entity(session, entity_ref)
    await given_facts(
        session,
        build_fact(entity_ref, stat_date=date(2026, 3, 7), spend_minor=999_000),
        build_fact(entity_ref, stat_date=date(2026, 3, 8), spend_minor=1000),
        build_fact(entity_ref, stat_date=date(2026, 3, 14), spend_minor=2000),
    )

    window = await SqlMetricWindowRepository(session).fetch_window(
        entity_ref=entity_ref, span=WindowSpan.D7, as_of=AS_OF
    )

    assert window.spend_minor == 3000


async def test_window_does_not_mix_two_entities(session: AsyncSession) -> None:
    mine = campaign_ref("win-mia")
    other = campaign_ref("win-ajena")
    await seed_entity(session, mine)
    await seed_entity(session, other)
    await given_facts(
        session,
        build_fact(mine, stat_date=date(2026, 3, 14), spend_minor=1000),
        build_fact(other, stat_date=date(2026, 3, 14), spend_minor=500_000),
    )

    window = await SqlMetricWindowRepository(session).fetch_window(
        entity_ref=mine, span=WindowSpan.D7, as_of=AS_OF
    )

    assert window.spend_minor == 1000


async def test_window_without_facts_is_empty_not_an_error(session: AsyncSession) -> None:
    entity_ref = campaign_ref("win-vacia")
    await seed_entity(session, entity_ref)

    window = await SqlMetricWindowRepository(session).fetch_window(
        entity_ref=entity_ref, span=WindowSpan.D7, as_of=AS_OF
    )

    assert window.spend_minor == 0
    assert window.impressions == 0
    assert window.search_lost_is_budget_pct is None


async def test_lost_impression_share_is_weighted_by_impressions(session: AsyncSession) -> None:
    entity_ref = campaign_ref("win-cuota")
    await seed_entity(session, entity_ref)
    await given_facts(
        session,
        build_fact(
            entity_ref,
            stat_date=date(2026, 3, 13),
            impressions=1000,
            search_lost_is_budget_pct=10.0,
        ),
        build_fact(
            entity_ref,
            stat_date=date(2026, 3, 14),
            impressions=3000,
            search_lost_is_budget_pct=50.0,
        ),
    )

    window = await SqlMetricWindowRepository(session).fetch_window(
        entity_ref=entity_ref, span=WindowSpan.D7, as_of=AS_OF
    )

    assert window.search_lost_is_budget_pct == pytest.approx(40.0)


async def test_same_weekday_series_comes_back_oldest_first(session: AsyncSession) -> None:
    entity_ref = campaign_ref("serie-orden")
    await seed_entity(session, entity_ref)
    saturdays = [date(2026, 2, 21), date(2026, 2, 28), date(2026, 3, 7)]
    await given_facts(
        session,
        *[
            build_fact(entity_ref, stat_date=day, spend_minor=(index + 1) * 100)
            for index, day in enumerate(saturdays)
        ],
        build_fact(entity_ref, stat_date=date(2026, 3, 10), spend_minor=777),
    )

    series = await SqlDailySpendSeriesRepository(session).fetch_same_weekday_series(
        entity_ref=entity_ref, as_of=date(2026, 3, 14), weeks=26
    )

    assert list(series) == [100.0, 200.0, 300.0]


async def test_same_weekday_series_keeps_the_last_weeks_only(session: AsyncSession) -> None:
    entity_ref = campaign_ref("serie-tope")
    await seed_entity(session, entity_ref)
    await given_facts(
        session,
        *[
            build_fact(entity_ref, stat_date=date(2026, 2, 21), spend_minor=100),
            build_fact(entity_ref, stat_date=date(2026, 2, 28), spend_minor=200),
            build_fact(entity_ref, stat_date=date(2026, 3, 7), spend_minor=300),
        ],
    )

    series = await SqlDailySpendSeriesRepository(session).fetch_same_weekday_series(
        entity_ref=entity_ref, as_of=date(2026, 3, 14), weeks=2
    )

    assert list(series) == [200.0, 300.0]


async def test_series_excludes_the_day_being_judged(session: AsyncSession) -> None:
    entity_ref = campaign_ref("serie-hoy")
    await seed_entity(session, entity_ref)
    await given_facts(
        session,
        build_fact(entity_ref, stat_date=date(2026, 3, 7), spend_minor=100),
        build_fact(entity_ref, stat_date=date(2026, 3, 14), spend_minor=900),
    )
    repository = SqlDailySpendSeriesRepository(session)

    series = await repository.fetch_same_weekday_series(
        entity_ref=entity_ref, as_of=date(2026, 3, 14), weeks=26
    )
    today = await repository.fetch_today_value(entity_ref=entity_ref, as_of=date(2026, 3, 14))

    assert list(series) == [100.0]
    assert today == 900.0


async def test_today_without_facts_is_zero(session: AsyncSession) -> None:
    entity_ref = campaign_ref("serie-sin-hoy")
    await seed_entity(session, entity_ref)

    today = await SqlDailySpendSeriesRepository(session).fetch_today_value(
        entity_ref=entity_ref, as_of=date(2026, 3, 14)
    )

    assert today == 0.0
