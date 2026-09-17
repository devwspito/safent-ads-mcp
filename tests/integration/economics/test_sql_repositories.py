"""Repositorios SQL de `economics` contra Postgres real (migracion
0016_economics): los dobles no modelan el trigger de solo-anexable ni el
UPSERT de `lag_curve_snapshots` (data-model.md §Migration plan, "causa raiz
numero uno en oposads")."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.economics.domain.identifiers import ProductId, UnitEconomicsProfileId
from safent_ads.economics.domain.lag_curve import LagCurve, LagObservation
from safent_ads.economics.domain.money import Money
from safent_ads.economics.domain.platform_divergence import PlatformDivergence
from safent_ads.economics.domain.rate import Rate, Theta
from safent_ads.economics.domain.unit_economics import UnitEconomicsProfile
from safent_ads.economics.infrastructure.sql_repositories import (
    SqlLagCurveRepository,
    SqlPlatformDivergenceRepository,
    SqlUnitEconomicsProfileRepository,
)
from safent_ads.shared.ids import BusinessId
from tests.conftest import BusinessFactory

pytestmark = pytest.mark.integration


async def _make_offering(session: AsyncSession, business_id: uuid.UUID) -> uuid.UUID:
    offering_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO offerings (id, business_id, code, title) "
            "VALUES (:id, :business_id, :code, 'Oferta de prueba')"
        ),
        {"id": offering_id, "business_id": business_id, "code": f"off-{offering_id.hex[:10]}"},
    )
    return offering_id


async def _make_platform_account(session: AsyncSession, business_id: uuid.UUID) -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO platform_accounts (id, business_id, platform, external_account_id, "
            "currency, timezone, api_tier, status) "
            "VALUES (:id, :business_id, 'google', :external_id, 'EUR', 'Europe/Madrid', "
            "'standard', 'ACTIVE')"
        ),
        {"id": account_id, "business_id": business_id, "external_id": f"act_{account_id.hex[:10]}"},
    )
    return account_id


def _profile(
    *, business_id: BusinessId, product_id: ProductId, version: int, effective_from: date
) -> UnitEconomicsProfile:
    return UnitEconomicsProfile.create(
        profile_id=UnitEconomicsProfileId.new(),
        business_id=business_id,
        product_id=product_id,
        version=version,
        effective_from=effective_from,
        list_price=Money.of("1200"),
        vat_rate=Rate.zero(),
        discount_rate=Rate.of("0.08"),
        refund_rate=Rate.of("0.06"),
        delivery_cost=Money.of("90"),
        sales_cost_per_close=Money.of("140"),
        collection_rate=Rate.of("0.92"),
        cvr_lead_to_business_conversion=Rate.of("0.045"),
        theta=Theta(Decimal("0.35")),
        margin_horizon_days=90,
    )


class TestUnitEconomicsProfileRepository:
    async def test_round_trips_current_profile(
        self, db_session: AsyncSession, business_factory: BusinessFactory
    ) -> None:
        business_id = BusinessId.parse(str(await business_factory.create()))
        offering_id = await _make_offering(db_session, business_id.value)
        product_id = ProductId(offering_id)
        repository = SqlUnitEconomicsProfileRepository(db_session)
        profile = _profile(
            business_id=business_id,
            product_id=product_id,
            version=1,
            effective_from=date(2026, 1, 1),
        )

        await repository.save(profile)
        reloaded = await repository.get_current(
            business_id=business_id, product_id=product_id, as_of=date(2026, 6, 1)
        )

        assert reloaded is not None
        assert reloaded.contribution_margin() == Money.of("724.74")
        assert reloaded.version == 1

    async def test_get_current_picks_latest_version_as_of_date(
        self, db_session: AsyncSession, business_factory: BusinessFactory
    ) -> None:
        business_id = BusinessId.parse(str(await business_factory.create()))
        offering_id = await _make_offering(db_session, business_id.value)
        product_id = ProductId(offering_id)
        repository = SqlUnitEconomicsProfileRepository(db_session)
        await repository.save(
            _profile(
                business_id=business_id,
                product_id=product_id,
                version=1,
                effective_from=date(2026, 1, 1),
            )
        )
        await repository.save(
            _profile(
                business_id=business_id,
                product_id=product_id,
                version=2,
                effective_from=date(2026, 6, 1),
            )
        )

        as_of_early = await repository.get_current(
            business_id=business_id, product_id=product_id, as_of=date(2026, 3, 1)
        )
        as_of_late = await repository.get_current(
            business_id=business_id, product_id=product_id, as_of=date(2026, 12, 1)
        )
        versions = await repository.list_versions(business_id=business_id, product_id=product_id)

        assert as_of_early is not None
        assert as_of_early.version == 1
        assert as_of_late is not None
        assert as_of_late.version == 2
        assert [p.version for p in versions] == [1, 2]

    async def test_table_is_append_only(
        self, db_session: AsyncSession, business_factory: BusinessFactory
    ) -> None:
        business_id = BusinessId.parse(str(await business_factory.create()))
        offering_id = await _make_offering(db_session, business_id.value)
        product_id = ProductId(offering_id)
        repository = SqlUnitEconomicsProfileRepository(db_session)
        await repository.save(
            _profile(
                business_id=business_id,
                product_id=product_id,
                version=1,
                effective_from=date(2026, 1, 1),
            )
        )

        with pytest.raises(DBAPIError, match="append-only"):
            await db_session.execute(
                text("UPDATE unit_economics_profiles SET version = 99 WHERE product_id = :pid"),
                {"pid": offering_id},
            )


class TestLagCurveRepository:
    async def test_save_upserts_by_product_and_platform(
        self, db_session: AsyncSession, business_factory: BusinessFactory
    ) -> None:
        business_id = BusinessId.parse(str(await business_factory.create()))
        offering_id = await _make_offering(db_session, business_id.value)
        product_id = ProductId(offering_id)
        repository = SqlLagCurveRepository(db_session)
        observations = [LagObservation(1, True)] * 20 + [LagObservation(5, True)] * 30
        first_curve = LagCurve.from_observations(observations, d_max=30)

        await repository.save(
            business_id=business_id, product_id=product_id, platform="google", curve=first_curve
        )
        second_curve = LagCurve.from_observations(observations * 2, d_max=30)
        await repository.save(
            business_id=business_id, product_id=product_id, platform="google", curve=second_curve
        )
        reloaded = await repository.get_current(
            business_id=business_id, product_id=product_id, platform="google"
        )

        assert reloaded is not None
        assert reloaded.sample_size == 100  # la segunda escritura sustituye, no duplica
        row_count = await db_session.scalar(
            text(
                "SELECT count(*) FROM lag_curve_snapshots "
                "WHERE product_id = :pid AND platform = 'google'"
            ),
            {"pid": offering_id},
        )
        assert row_count == 1

    async def test_most_mature_for_business_picks_the_largest_sample(
        self, db_session: AsyncSession, business_factory: BusinessFactory
    ) -> None:
        """T157: `orchestration` no conoce el producto de cada campana
        todavia -- resuelve el rezago con la curva mas madura del negocio
        para esa plataforma en vez de una constante."""
        business_id = BusinessId.parse(str(await business_factory.create()))
        small_offering = await _make_offering(db_session, business_id.value)
        large_offering = await _make_offering(db_session, business_id.value)
        repository = SqlLagCurveRepository(db_session)
        small_curve = LagCurve.from_observations([LagObservation(1, True)] * 20, d_max=30)
        large_curve = LagCurve.from_observations([LagObservation(1, True)] * 150, d_max=30)

        await repository.save(
            business_id=business_id,
            product_id=ProductId(small_offering),
            platform="google",
            curve=small_curve,
        )
        await repository.save(
            business_id=business_id,
            product_id=ProductId(large_offering),
            platform="google",
            curve=large_curve,
        )

        most_mature = await repository.get_most_mature_for_business(
            business_id=business_id, platform="google"
        )

        assert most_mature is not None
        assert most_mature.sample_size == 150

    async def test_most_mature_for_business_without_any_curve_is_none(
        self, db_session: AsyncSession, business_factory: BusinessFactory
    ) -> None:
        business_id = BusinessId.parse(str(await business_factory.create()))
        repository = SqlLagCurveRepository(db_session)

        most_mature = await repository.get_most_mature_for_business(
            business_id=business_id, platform="google"
        )

        assert most_mature is None


class TestPlatformDivergenceRepository:
    async def test_history_orders_latest_first(
        self, db_session: AsyncSession, business_factory: BusinessFactory
    ) -> None:
        business_id = BusinessId.parse(str(await business_factory.create()))
        account_id = await _make_platform_account(db_session, business_id.value)
        repository = SqlPlatformDivergenceRepository(db_session)
        older = PlatformDivergence.compute(
            crm_conversions=100,
            platform_conversions=100,
            window_start=date(2026, 1, 1),
            window_end=date(2026, 2, 26),
        )
        newer = PlatformDivergence.compute(
            crm_conversions=140,
            platform_conversions=100,
            window_start=date(2026, 2, 26),
            window_end=date(2026, 4, 23),
        )

        await repository.save(
            business_id=business_id, platform_account_id=str(account_id), divergence=older
        )
        await repository.save(
            business_id=business_id, platform_account_id=str(account_id), divergence=newer
        )
        latest = await repository.get_latest(
            business_id=business_id, platform_account_id=str(account_id)
        )
        previous = await repository.get_previous(
            business_id=business_id, platform_account_id=str(account_id)
        )

        assert latest is not None
        assert previous is not None
        assert latest.crm_conversions == 140
        assert previous.crm_conversions == 100
        assert latest.is_jump_anomaly(previous) is True

    async def test_window_check_constraint_rejects_inverted_window(
        self, db_session: AsyncSession, business_factory: BusinessFactory
    ) -> None:
        business_id = BusinessId.parse(str(await business_factory.create()))
        account_id = await _make_platform_account(db_session, business_id.value)

        with pytest.raises(DBAPIError, match="platform_divergence_window_check"):
            await db_session.execute(
                text(
                    "INSERT INTO platform_divergence_snapshots "
                    "(business_id, platform_account_id, crm_conversions, "
                    "platform_conversions, shrinkage_m, value, window_start, window_end) "
                    "VALUES (:business_id, :account_id, 10, 10, 10, 1.0, "
                    "'2026-02-01', '2026-01-01')"
                ),
                {"business_id": business_id.value, "account_id": account_id},
            )
