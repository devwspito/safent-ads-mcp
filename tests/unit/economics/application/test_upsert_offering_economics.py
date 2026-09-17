"""`UpsertOfferingEconomics`/`ListOfferingsWithEconomics` (T131/T132) contra
`InMemoryOfferingEconomicsRepository` (application layer tested with
in-memory adapters)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from safent_ads.economics.application.errors import (
    InvalidOfferingEconomicsError,
    OfferingNotFoundError,
)
from safent_ads.economics.application.list_offerings import ListOfferingsWithEconomics
from safent_ads.economics.application.ports import OfferingEconomicsInput, OfferingSummary
from safent_ads.economics.application.upsert_offering_economics import UpsertOfferingEconomics
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.economics.domain.money import Money
from safent_ads.economics.domain.payment_plan import PaymentPlan
from safent_ads.economics.testing.in_memory_repositories import (
    InMemoryOfferingEconomicsRepository,
)
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()
_OFFERING_ID = ProductId(uuid.uuid4())


def _valid_input(**overrides: object) -> OfferingEconomicsInput:
    fields: dict[str, object] = {
        "vat_rate_pct": Decimal("21"),
        "delivery_cost_minor": 9_000,
        "sales_cost_minor": 14_000,
        "refund_rate_pct": Decimal("6"),
        "payment_plan": PaymentPlan.NONE,
        "currency": "EUR",
    }
    fields.update(overrides)
    return OfferingEconomicsInput(**fields)  # type: ignore[arg-type]


def _repository_with_offering() -> InMemoryOfferingEconomicsRepository:
    repo = InMemoryOfferingEconomicsRepository()
    repo.seed_offering(
        business_id=_BUSINESS_ID,
        offering=OfferingSummary(
            offering_id=str(_OFFERING_ID),
            code="off-1",
            title="Plan Anual Pro",
            is_active=True,
            list_price=Money.of("1200"),
            economics=None,
        ),
    )
    return repo


async def test_upsert_fills_economics_and_list_offerings_stops_being_provisional() -> None:
    repo = _repository_with_offering()
    use_case = UpsertOfferingEconomics(offerings=repo)

    saved = await use_case.execute(
        business_id=_BUSINESS_ID, offering_id=_OFFERING_ID, economics=_valid_input()
    )

    assert saved.vat_rate_pct == Decimal("21")
    offerings = await ListOfferingsWithEconomics(offerings=repo).execute(
        business_id=_BUSINESS_ID
    )
    assert offerings[0].economics is not None


async def test_list_offerings_without_economics_is_none() -> None:
    repo = _repository_with_offering()

    offerings = await ListOfferingsWithEconomics(offerings=repo).execute(
        business_id=_BUSINESS_ID
    )

    assert offerings[0].economics is None


async def test_upsert_rejects_unknown_offering() -> None:
    use_case = UpsertOfferingEconomics(offerings=InMemoryOfferingEconomicsRepository())

    with pytest.raises(OfferingNotFoundError):
        await use_case.execute(
            business_id=_BUSINESS_ID, offering_id=_OFFERING_ID, economics=_valid_input()
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"vat_rate_pct": Decimal("-1")},
        {"vat_rate_pct": Decimal("100.01")},
        {"refund_rate_pct": Decimal("-0.01")},
        {"refund_rate_pct": Decimal("100.5")},
        {"delivery_cost_minor": -1},
        {"sales_cost_minor": -1},
        {"currency": "eur"},
        {"currency": "EURO"},
    ],
)
async def test_upsert_rejects_out_of_range_values(overrides: dict[str, object]) -> None:
    repo = _repository_with_offering()
    use_case = UpsertOfferingEconomics(offerings=repo)

    with pytest.raises(InvalidOfferingEconomicsError):
        await use_case.execute(
            business_id=_BUSINESS_ID,
            offering_id=_OFFERING_ID,
            economics=_valid_input(**overrides),
        )


async def test_upsert_allows_refund_rate_none() -> None:
    repo = _repository_with_offering()
    use_case = UpsertOfferingEconomics(offerings=repo)

    saved = await use_case.execute(
        business_id=_BUSINESS_ID,
        offering_id=_OFFERING_ID,
        economics=_valid_input(refund_rate_pct=None),
    )

    assert saved.refund_rate_pct is None
