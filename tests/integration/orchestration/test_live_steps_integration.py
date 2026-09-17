"""`LiveIngestionStep`/`LiveSignalStep`/`SqlBusinessListing` contra Postgres
real: la traduccion `MetricFactSnapshot` -> `MetricFact` y el UPSERT en
`metrics_daily` son la parte que un doble en memoria no puede probar
(columnas, tipos, FKs compuestas -- C-27)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from safent_ads.accounts.application.ports import (
    AccountRef,
    AdEntitySnapshot,
    AssetUploadRequest,
    EntityStateSnapshot,
    MetricFactSnapshot,
    MetricsRequest,
    PlatformAssetHandle,
    SignedAuthorization,
    WriteIntent,
    WriteOutcome,
)
from safent_ads.accounts.domain.ad_entity import AdEntityStatus
from safent_ads.accounts.domain.learning_state import LearningState
from safent_ads.accounts.domain.money import Money
from safent_ads.accounts.domain.refs import IdempotencyKey
from safent_ads.orchestration.infrastructure.live_steps import (
    LiveIngestionStep,
    LiveSignalStep,
    SqlBusinessListing,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityRef
from tests.contracts.sql_fixtures import campaign_ref, seed_entity

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)


class _FakePlatformPort:
    """`AdsPlatformPort` de contornos minimos: un inventario de una sola
    entidad (la ya sembrada por `seed_entity`, para que `SyncAccountInventory`
    haga un UPSERT trivial) y un hecho de metricas de hoy."""

    def __init__(self, entity_ref: EntityRef) -> None:
        self._entity_ref = entity_ref

    async def fetch_account_inventory(
        self, account_ref: AccountRef
    ) -> Sequence[AdEntitySnapshot]:
        del account_ref
        return [
            AdEntitySnapshot(
                entity_ref=self._entity_ref,
                parent_ref=EntityRef(
                    self._entity_ref.platform, self._entity_ref.level, "account-parent"
                ),
                name="Campana de integracion",
                status=AdEntityStatus.ACTIVE,
                is_controllable=True,
                learning_state=LearningState.NOT_APPLICABLE,
                budget=None,
                bid_target=None,
                shared_budget_ref=None,
                canonical_state={"name": "Campana de integracion", "status": "ACTIVE"},
                fetched_at=_NOW,
            )
        ]

    async def fetch_metrics(self, request: MetricsRequest) -> Sequence[MetricFactSnapshot]:
        del request
        return [
            MetricFactSnapshot(
                entity_ref=self._entity_ref,
                stat_date=_NOW.date(),
                stat_hour=None,
                currency="EUR",
                spend=Money(4_200, "EUR"),
                impressions=1_000,
                clicks=50,
                reach=None,
                frequency=None,
                conversions_by_kind={"lead": 3},
                conversion_value=None,
                video_views_3s=None,
                video_views_75pct=None,
                search_lost_is_budget=None,
                search_lost_is_rank=None,
            )
        ]

    async def read_entity_state(self, entity_ref: EntityRef) -> EntityStateSnapshot:
        raise NotImplementedError

    async def upload_asset(self, request: AssetUploadRequest) -> PlatformAssetHandle:
        raise NotImplementedError

    async def execute_write(
        self,
        intent: WriteIntent,
        authorization: SignedAuthorization,
        idempotency_key: IdempotencyKey,
    ) -> WriteOutcome:
        raise NotImplementedError


@pytest.fixture
async def session_factory(
    database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def seeded_business(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[uuid.UUID, EntityRef]]:
    entity_ref = campaign_ref(f"live{uuid.uuid4().hex[:8]}", platform_value="google")
    async with session_factory() as session:
        business_id = await seed_entity(session, entity_ref)
        await session.commit()
    try:
        yield business_id, entity_ref
    finally:
        async with session_factory() as session:
            # Orden de FK: hechos/senales -> entidades -> cuentas -> negocio
            # (`credential_refs` no lleva `business_id`, se limpia por la
            # cuenta que la referencia).
            params = {"id": business_id}
            await session.execute(
                text("DELETE FROM metrics_daily WHERE business_id = :id"), params
            )
            await session.execute(text("DELETE FROM anomalies WHERE business_id = :id"), params)
            await session.execute(text("DELETE FROM signals WHERE business_id = :id"), params)
            await session.execute(
                text("DELETE FROM ad_entities WHERE business_id = :id"), params
            )
            credential_ref_ids = (
                await session.execute(
                    text(
                        "SELECT credential_ref_id FROM platform_accounts WHERE business_id = :id"
                    ),
                    params,
                )
            ).scalars().all()
            await session.execute(
                text("DELETE FROM platform_accounts WHERE business_id = :id"), params
            )
            if credential_ref_ids:
                await session.execute(
                    text("DELETE FROM credential_refs WHERE id = ANY(:ids)"),
                    {"ids": list(credential_ref_ids)},
                )
            await session.execute(text("DELETE FROM businesses WHERE id = :id"), params)
            await session.commit()


async def test_business_listing_includes_seeded_active_business(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_business: tuple[uuid.UUID, EntityRef],
) -> None:
    business_id, _entity_ref = seeded_business

    active_ids = await SqlBusinessListing(session_factory).list_active_business_ids()

    assert BusinessId(business_id) in active_ids


async def test_ingestion_step_upserts_today_metric_fact(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_business: tuple[uuid.UUID, EntityRef],
) -> None:
    business_id, entity_ref = seeded_business
    step = LiveIngestionStep(
        session_factory, _FakePlatformPort(entity_ref), FixedClock(_NOW)
    )

    await step.run(BusinessId(business_id), "cycle-1", _NOW)

    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT spend, conversions_lead FROM metrics_daily "
                    "WHERE entity_ref = :entity_ref AND stat_date = :stat_date"
                ),
                {"entity_ref": str(entity_ref), "stat_date": _NOW.date()},
            )
        ).mappings().one()
    assert int(row["spend"]) == 4_200
    assert int(row["conversions_lead"]) == 3


async def test_ingestion_step_is_idempotent_on_retry(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_business: tuple[uuid.UUID, EntityRef],
) -> None:
    business_id, entity_ref = seeded_business
    step = LiveIngestionStep(
        session_factory, _FakePlatformPort(entity_ref), FixedClock(_NOW)
    )

    await step.run(BusinessId(business_id), "cycle-1", _NOW)
    await step.run(BusinessId(business_id), "cycle-1", _NOW)  # reintento: mismo hecho

    async with session_factory() as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM metrics_daily "
                    "WHERE entity_ref = :entity_ref AND stat_date = :stat_date"
                ),
                {"entity_ref": str(entity_ref), "stat_date": _NOW.date()},
            )
        ).scalar_one()
    assert count == 1


async def test_signal_step_runs_without_raising_on_fresh_entity(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_business: tuple[uuid.UUID, EntityRef],
) -> None:
    """Sin 8 semanas de historial, `same_weekday_z` divide por una serie
    vacia -- el paso debe absorberlo (documentado en `live_steps.py`), no
    hacer fallar el ciclo del negocio."""
    business_id, _entity_ref = seeded_business
    step = LiveSignalStep(session_factory, FixedClock(_NOW))

    await step.run(BusinessId(business_id), str(uuid.uuid4()), _NOW)
