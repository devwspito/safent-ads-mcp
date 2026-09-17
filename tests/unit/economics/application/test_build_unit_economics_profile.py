"""`BuildUnitEconomicsProfile` (T156): perfil confirmado cuando hay entradas
del dueno y CRM suficiente; provisional cuando falta cualquiera de las dos
(profitability-engine.md §1: 'el motor no propone ninguna subida de gasto
sobre un perfil provisional')."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.domain.lead_attribution import (
    AttributionRung,
    LeadAttribution,
    LeadAttributionId,
)
from safent_ads.crm.testing.in_memory_repositories import InMemoryLeadAttributionRepository
from safent_ads.economics.application.build_unit_economics_profile import (
    BuildUnitEconomicsProfile,
)
from safent_ads.economics.application.errors import OfferingPriceMissingError
from safent_ads.economics.application.ports import MarginInputs
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.economics.domain.money import Money
from safent_ads.economics.domain.rate import Rate, Theta
from safent_ads.economics.domain.unit_economics import ProfileStatus
from safent_ads.economics.testing.in_memory_repositories import (
    InMemoryCalendarEventLookupPort,
    InMemoryMarginInputsPort,
    InMemoryOfferingPricePort,
    InMemoryUnitEconomicsProfileRepository,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()
_PRODUCT_ID = ProductId.parse("11111111-1111-4111-8111-111111111111")
_CALENDAR_EVENT_ID = "22222222-2222-4222-8222-222222222222"
_TODAY = datetime(2026, 3, 1, tzinfo=UTC)
_DEFAULT_LIST_PRICE = Money.of("1200")


def _margin_inputs() -> MarginInputs:
    # Mismos numeros que el ejemplo de profitability-engine.md §3 (Secundaria
    # Matematicas): impartir 90 EUR, comercial 140 EUR/cierre -- con un solo
    # cierre en el mes sintetico de este test, monthly_sales_team_cost=140
    # produce exactamente ese coste por cierre.
    return MarginInputs(
        vat_rate=Rate.zero(),
        delivery_cost=Money.of("90"),
        monthly_sales_team_cost=Money.of("140"),
        theta=Theta(Decimal("0.35")),
        margin_horizon_days=90,
    )


def _identity(raw: str) -> HashedIdentity:
    return HashedIdentity.compute(business_id=_BUSINESS_ID, raw_identifier=raw, salt="s")


def _lead(raw: str, occurred_at: datetime) -> LeadAttribution:
    return LeadAttribution(
        lead_attribution_id=LeadAttributionId.new(),
        business_id=_BUSINESS_ID,
        hashed_identity=_identity(raw),
        entity_ref=None,
        attribution_rung=AttributionRung.AGGREGATE,
        conversion_kind=ConversionKind.LEAD,
        value_minor=0,
        occurred_at=occurred_at,
        observed_at=occurred_at,
        calendar_event_id=_CALENDAR_EVENT_ID,
    )


def _conversion(raw: str, occurred_at: datetime, value_minor: int) -> LeadAttribution:
    return LeadAttribution(
        lead_attribution_id=LeadAttributionId.new(),
        business_id=_BUSINESS_ID,
        hashed_identity=_identity(raw),
        entity_ref=None,
        attribution_rung=AttributionRung.AGGREGATE,
        conversion_kind=ConversionKind.BUSINESS_CONVERSION,
        value_minor=value_minor,
        occurred_at=occurred_at,
        observed_at=occurred_at,
        calendar_event_id=_CALENDAR_EVENT_ID,
    )


async def _build_use_case(
    *,
    price: Money | None = _DEFAULT_LIST_PRICE,
    margin_inputs: MarginInputs | None = None,
    attributions: list[LeadAttribution] | None = None,
) -> tuple[BuildUnitEconomicsProfile, InMemoryUnitEconomicsProfileRepository]:
    profiles = InMemoryUnitEconomicsProfileRepository()
    offering_prices = InMemoryOfferingPricePort()
    if price is not None:
        offering_prices.seed(business_id=_BUSINESS_ID, product_id=_PRODUCT_ID, list_price=price)
    margin_port = InMemoryMarginInputsPort()
    if margin_inputs is not None:
        margin_port.seed(
            business_id=_BUSINESS_ID, product_id=_PRODUCT_ID, margin_inputs=margin_inputs
        )
    calendar_events = InMemoryCalendarEventLookupPort()
    calendar_events.seed(
        business_id=_BUSINESS_ID, product_id=_PRODUCT_ID, calendar_event_ids=[_CALENDAR_EVENT_ID]
    )
    lead_attributions = InMemoryLeadAttributionRepository()
    for a in attributions or []:
        await lead_attributions.save(a)
    use_case = BuildUnitEconomicsProfile(
        profiles=profiles,
        margin_inputs=margin_port,
        offering_prices=offering_prices,
        calendar_events=calendar_events,
        lead_attributions=lead_attributions,
        clock=FixedClock(_TODAY),
    )
    return use_case, profiles


async def test_without_owner_margin_inputs_the_profile_is_provisional() -> None:
    use_case, profiles = await _build_use_case(margin_inputs=None)

    profile = await use_case.execute(business_id=_BUSINESS_ID, product_id=_PRODUCT_ID)

    assert profile.status is ProfileStatus.PROVISIONAL
    assert profiles.saved == [profile]


async def test_without_a_list_price_raises_instead_of_guessing() -> None:
    use_case, _ = await _build_use_case(price=None, margin_inputs=_margin_inputs())

    with pytest.raises(OfferingPriceMissingError):
        await use_case.execute(business_id=_BUSINESS_ID, product_id=_PRODUCT_ID)


async def test_without_enough_crm_conversions_the_profile_is_provisional() -> None:
    use_case, _ = await _build_use_case(margin_inputs=_margin_inputs(), attributions=[])

    profile = await use_case.execute(business_id=_BUSINESS_ID, product_id=_PRODUCT_ID)

    assert profile.status is ProfileStatus.PROVISIONAL


async def test_with_owner_inputs_and_crm_history_the_profile_is_confirmed() -> None:
    attributions = [
        _lead("a@x.com", datetime(2026, 1, 1, tzinfo=UTC)),
        _lead("b@x.com", datetime(2026, 1, 2, tzinfo=UTC)),
        _conversion("a@x.com", datetime(2026, 1, 10, tzinfo=UTC), value_minor=110_000),
    ]
    use_case, profiles = await _build_use_case(
        margin_inputs=_margin_inputs(), attributions=attributions
    )

    profile = await use_case.execute(business_id=_BUSINESS_ID, product_id=_PRODUCT_ID)

    assert profile.status is ProfileStatus.CONFIRMED
    assert profile.cvr_lead_to_business_conversion.value == Decimal("0.5")
    assert profiles.saved == [profile]


async def test_running_twice_the_same_day_is_idempotent() -> None:
    attributions = [
        _lead("a@x.com", datetime(2026, 1, 1, tzinfo=UTC)),
        _conversion("a@x.com", datetime(2026, 1, 10, tzinfo=UTC), value_minor=110_000),
    ]
    use_case, profiles = await _build_use_case(
        margin_inputs=_margin_inputs(), attributions=attributions
    )

    first = await use_case.execute(business_id=_BUSINESS_ID, product_id=_PRODUCT_ID)
    second = await use_case.execute(business_id=_BUSINESS_ID, product_id=_PRODUCT_ID)

    assert first.profile_id == second.profile_id
    assert len(profiles.saved) == 1
