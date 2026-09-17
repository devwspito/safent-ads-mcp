"""Banco de contrato de `MetricFactRepository`/`RestatementRepository`:
mismos casos contra el doble en memoria y contra Postgres real."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.metrics.application.ports import MetricFactRepository, RestatementRepository
from safent_ads.metrics.domain.conversion_kind import ConversionKind
from safent_ads.metrics.domain.metric_fact import MetricFact
from safent_ads.metrics.infrastructure.sql_repositories import (
    SqlMetricFactRepository,
    SqlRestatementRepository,
)
from safent_ads.metrics.testing.in_memory_metric_fact_repository import (
    InMemoryMetricFactRepository,
)
from safent_ads.metrics.testing.in_memory_restatement_repository import (
    InMemoryRestatementRepository,
)
from safent_ads.shared.ids import EntityRef
from tests.conftest import rolled_back_session
from tests.contracts.sql_fixtures import campaign_ref, seed_entity

INGESTED_AT = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


@dataclass(slots=True)
class MetricsFixture:
    facts: MetricFactRepository
    restatements: RestatementRepository
    session: AsyncSession | None

    async def given_entity(self, entity_ref: EntityRef) -> None:
        if self.session is not None:
            await seed_entity(self.session, entity_ref)


@pytest.fixture(
    params=[
        pytest.param("in_memory", id="in_memory"),
        pytest.param("sql", id="sql", marks=pytest.mark.integration),
    ]
)
async def metrics(request: pytest.FixtureRequest) -> AsyncIterator[MetricsFixture]:
    if request.param == "in_memory":
        yield MetricsFixture(
            facts=InMemoryMetricFactRepository(),
            restatements=InMemoryRestatementRepository(),
            session=None,
        )
        return
    database_url: str = request.getfixturevalue("database_url")
    async with rolled_back_session(database_url) as session:
        yield MetricsFixture(
            facts=SqlMetricFactRepository(session),
            restatements=SqlRestatementRepository(session),
            session=session,
        )


def build_fact(
    entity_ref: EntityRef,
    *,
    stat_date: datetime | None = None,
    day: int = 14,
    stat_hour: int | None = None,
    spend_minor: int = 7800,
    impressions: int = 12000,
    clicks: int = 240,
    conversions: dict[ConversionKind, int] | None = None,
    search_lost_is_budget_pct: float | None = None,
    revision: int = 1,
) -> MetricFact:
    del stat_date
    return MetricFact(
        entity_ref=entity_ref,
        stat_date=datetime(2026, 3, day, tzinfo=UTC).date(),
        stat_hour=stat_hour,
        account_timezone="Europe/Madrid",
        currency="EUR",
        spend_minor=spend_minor,
        impressions=impressions,
        clicks=clicks,
        reach=9000,
        conversions=MappingProxyType(
            conversions if conversions is not None else {ConversionKind.LEAD: 12}
        ),
        conversion_value_minor=45000,
        video_views_3s=3000,
        video_views_75pct=900,
        search_lost_is_budget_pct=search_lost_is_budget_pct,
        ingested_at=INGESTED_AT,
        revision=revision,
    )


__all__ = ["INGESTED_AT", "MetricsFixture", "build_fact", "campaign_ref", "metrics"]
