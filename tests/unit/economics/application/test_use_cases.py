"""Casos de uso de `economics` contra los dobles en memoria de
`economics/testing` (patron `application layer tested with in-memory
adapters`)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from safent_ads.economics.application.errors import (
    LagCurveNotFoundError,
    PlatformDivergenceNotFoundError,
    UnitEconomicsProfileNotFoundError,
)
from safent_ads.economics.application.get_attribution_lag_curve import GetAttributionLagCurve
from safent_ads.economics.application.get_cohort_projection import GetCohortProjection
from safent_ads.economics.application.get_platform_divergence import GetPlatformDivergence
from safent_ads.economics.application.get_target_cpa import GetTargetCpa
from safent_ads.economics.application.get_unit_economics import GetUnitEconomics
from safent_ads.economics.domain.identifiers import ProductId, UnitEconomicsProfileId
from safent_ads.economics.domain.lag_curve import LagCurve, LagObservation
from safent_ads.economics.domain.money import Money
from safent_ads.economics.domain.platform_divergence import PlatformDivergence
from safent_ads.economics.domain.rate import Rate, Theta
from safent_ads.economics.domain.unit_economics import UnitEconomicsProfile
from safent_ads.economics.testing.in_memory_repositories import (
    InMemoryLagCurveRepository,
    InMemoryPlatformDivergenceRepository,
    InMemoryUnitEconomicsProfileRepository,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()
_PRODUCT_ID = ProductId(uuid.uuid4())


def _profile() -> UnitEconomicsProfile:
    return UnitEconomicsProfile.create(
        profile_id=UnitEconomicsProfileId.new(),
        business_id=_BUSINESS_ID,
        product_id=_PRODUCT_ID,
        version=1,
        effective_from=date(2026, 1, 1),
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


class TestGetUnitEconomics:
    async def test_returns_view_for_current_profile(self) -> None:
        repo = InMemoryUnitEconomicsProfileRepository()
        await repo.save(_profile())
        clock = FixedClock(datetime(2026, 6, 1, tzinfo=UTC))
        use_case = GetUnitEconomics(repo, clock)

        view = await use_case.execute(business_id=_BUSINESS_ID, product_id=_PRODUCT_ID)

        assert view.contribution_margin == Money.of("724.74")
        assert view.status == "confirmed"

    async def test_missing_profile_raises(self) -> None:
        repo = InMemoryUnitEconomicsProfileRepository()
        clock = FixedClock(datetime(2026, 6, 1, tzinfo=UTC))
        use_case = GetUnitEconomics(repo, clock)

        with pytest.raises(UnitEconomicsProfileNotFoundError):
            await use_case.execute(business_id=_BUSINESS_ID, product_id=_PRODUCT_ID)


class TestGetTargetCpa:
    async def test_returns_confidence_from_status(self) -> None:
        repo = InMemoryUnitEconomicsProfileRepository()
        await repo.save(_profile())
        clock = FixedClock(datetime(2026, 6, 1, tzinfo=UTC))
        use_case = GetTargetCpa(repo, clock)

        view = await use_case.execute(business_id=_BUSINESS_ID, product_id=_PRODUCT_ID)

        assert view.confidence == "confirmed"
        assert view.target_cost_per_conversion == Money.of("471.08")


class TestGetAttributionLagCurve:
    async def test_returns_curve_summary(self) -> None:
        repo = InMemoryLagCurveRepository()
        observations = [LagObservation(1, True)] * 20 + [LagObservation(5, True)] * 30
        curve = LagCurve.from_observations(observations, d_max=30)
        await repo.save(
            business_id=_BUSINESS_ID, product_id=_PRODUCT_ID, platform="google", curve=curve
        )
        use_case = GetAttributionLagCurve(repo)

        view = await use_case.execute(
            business_id=_BUSINESS_ID, product_id=_PRODUCT_ID, platform="google"
        )

        assert view.sample_size == 50
        assert view.median_lag_days == 5

    async def test_missing_curve_raises(self) -> None:
        use_case = GetAttributionLagCurve(InMemoryLagCurveRepository())
        with pytest.raises(LagCurveNotFoundError):
            await use_case.execute(
                business_id=_BUSINESS_ID, product_id=_PRODUCT_ID, platform="meta"
            )


class TestGetCohortProjection:
    async def test_returns_projection(self) -> None:
        repo = InMemoryLagCurveRepository()
        observations = [LagObservation(1, True)] * 20 + [LagObservation(10, True)] * 50
        curve = LagCurve.from_observations(observations, d_max=30)
        await repo.save(
            business_id=_BUSINESS_ID, product_id=_PRODUCT_ID, platform="google", curve=curve
        )
        use_case = GetCohortProjection(repo)

        view = await use_case.execute(
            business_id=_BUSINESS_ID,
            product_id=_PRODUCT_ID,
            platform="google",
            observed=10,
            age_days=1,
        )

        assert view.observed == 10
        assert view.projected > view.observed
        assert view.can_raise is False  # madurez baja a un dia de edad


class TestGetPlatformDivergence:
    async def test_returns_view_with_jump_flag(self) -> None:
        repo = InMemoryPlatformDivergenceRepository()
        previous = PlatformDivergence.compute(crm_conversions=100, platform_conversions=100)
        current = PlatformDivergence.compute(crm_conversions=140, platform_conversions=100)
        repo.seed(
            business_id=_BUSINESS_ID, platform_account_id="acc-1", snapshots=[previous, current]
        )
        use_case = GetPlatformDivergence(repo)

        view = await use_case.execute(business_id=_BUSINESS_ID, platform_account_id="acc-1")

        assert view.is_jump_anomaly is True

    async def test_missing_snapshot_raises(self) -> None:
        use_case = GetPlatformDivergence(InMemoryPlatformDivergenceRepository())
        with pytest.raises(PlatformDivergenceNotFoundError):
            await use_case.execute(business_id=_BUSINESS_ID, platform_account_id="unknown")
