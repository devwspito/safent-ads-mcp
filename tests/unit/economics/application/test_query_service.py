"""`EconomicsQueryService`: la fachada que usan MCP y REST."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from safent_ads.economics.application.query_service import EconomicsQueryService
from safent_ads.economics.domain.identifiers import ProductId, UnitEconomicsProfileId
from safent_ads.economics.domain.money import Money
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


async def test_unit_economics_and_target_cpa_share_the_same_profile() -> None:
    profiles = InMemoryUnitEconomicsProfileRepository()
    await profiles.save(
        UnitEconomicsProfile.create(
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
    )
    service = EconomicsQueryService(
        profiles=profiles,
        lag_curves=InMemoryLagCurveRepository(),
        divergences=InMemoryPlatformDivergenceRepository(),
        clock=FixedClock(datetime(2026, 6, 1, tzinfo=UTC)),
    )

    economics = await service.unit_economics(business_id=_BUSINESS_ID, product_id=_PRODUCT_ID)
    target_cpa = await service.target_cpa(business_id=_BUSINESS_ID, product_id=_PRODUCT_ID)

    assert economics.target_cost_per_conversion == target_cpa.target_cost_per_conversion
    assert target_cpa.confidence == "confirmed"
