"""`ComputeLagCurve` (T156): recalcula y materializa `LagCurve` desde las
observaciones crudas; sin observaciones, no sobreescribe con una curva vacia."""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.economics.application.compute_lag_curve import ComputeLagCurve
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.economics.domain.lag_curve import LagObservation
from safent_ads.economics.testing.in_memory_repositories import (
    InMemoryLagCurveRepository,
    InMemoryLagObservationRepository,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()
_PRODUCT_ID = ProductId.parse("11111111-1111-4111-8111-111111111111")
_PLATFORM = "google"


async def test_computes_and_saves_a_curve_from_observations() -> None:
    observations_repo = InMemoryLagObservationRepository()
    observations_repo.seed(
        business_id=_BUSINESS_ID,
        product_id=_PRODUCT_ID,
        platform=_PLATFORM,
        observations=[
            LagObservation(duration_days=5, converted=True),
            LagObservation(duration_days=10, converted=True),
            LagObservation(duration_days=30, converted=False),
        ],
    )
    curves_repo = InMemoryLagCurveRepository()
    use_case = ComputeLagCurve(
        observations_repo, curves_repo, FixedClock(datetime(2026, 3, 1, tzinfo=UTC))
    )

    curve = await use_case.execute(
        business_id=_BUSINESS_ID, product_id=_PRODUCT_ID, platform=_PLATFORM
    )

    assert curve is not None
    assert curve.sample_size == 3
    stored = await curves_repo.get_current(
        business_id=_BUSINESS_ID, product_id=_PRODUCT_ID, platform=_PLATFORM
    )
    assert stored is not None
    assert stored.sample_size == 3


async def test_without_observations_nothing_is_saved() -> None:
    observations_repo = InMemoryLagObservationRepository()
    curves_repo = InMemoryLagCurveRepository()
    use_case = ComputeLagCurve(
        observations_repo, curves_repo, FixedClock(datetime(2026, 3, 1, tzinfo=UTC))
    )

    curve = await use_case.execute(
        business_id=_BUSINESS_ID, product_id=_PRODUCT_ID, platform=_PLATFORM
    )

    assert curve is None
    assert (
        await curves_repo.get_current(
            business_id=_BUSINESS_ID, product_id=_PRODUCT_ID, platform=_PLATFORM
        )
        is None
    )


async def test_recomputing_with_the_same_data_is_idempotent() -> None:
    observations_repo = InMemoryLagObservationRepository()
    observations_repo.seed(
        business_id=_BUSINESS_ID,
        product_id=_PRODUCT_ID,
        platform=_PLATFORM,
        observations=[LagObservation(duration_days=5, converted=True)],
    )
    curves_repo = InMemoryLagCurveRepository()
    use_case = ComputeLagCurve(
        observations_repo, curves_repo, FixedClock(datetime(2026, 3, 1, tzinfo=UTC))
    )

    first = await use_case.execute(
        business_id=_BUSINESS_ID, product_id=_PRODUCT_ID, platform=_PLATFORM
    )
    second = await use_case.execute(
        business_id=_BUSINESS_ID, product_id=_PRODUCT_ID, platform=_PLATFORM
    )

    assert first is not None and second is not None
    assert first.as_cumulative_sequence() == second.as_cumulative_sequence()
