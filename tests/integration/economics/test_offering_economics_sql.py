"""`SqlOfferingEconomicsRepository`/`SqlMarginInputsPort` (T131/T132) sobre
`offering_economics` (0029_economics_inputs), Postgres real: FK compuesta
`(offering_id, business_id)` (IDOR a nivel de esquema), upsert, y la
traduccion pct/minor -> `Rate`/`Money` que `MarginInputsPort` necesita."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.economics.application.ports import OfferingEconomicsInput
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.economics.domain.payment_plan import PaymentPlan
from safent_ads.economics.infrastructure.offering_economics_sql import (
    SqlMarginInputsPort,
    SqlOfferingEconomicsRepository,
)
from safent_ads.shared.ids import BusinessId
from tests.conftest import BusinessFactory

pytestmark = pytest.mark.integration


async def _seed_offering(
    session: AsyncSession, business_id: uuid.UUID, *, price_amount: str | None = "1200"
) -> uuid.UUID:
    offering_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO offerings (id, business_id, code, title, price_amount, price_currency) "
            "VALUES (:id, :business_id, :code, 'Oferta', :price_amount, "
            + ("'EUR')" if price_amount is not None else "NULL)")
        ),
        {
            "id": offering_id,
            "business_id": business_id,
            "code": f"off-{offering_id.hex[:10]}",
            **({"price_amount": price_amount} if price_amount is not None else {}),
        },
    )
    return offering_id


def _input(**overrides: object) -> OfferingEconomicsInput:
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


async def test_offering_exists_is_scoped_to_the_owning_business(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_a = await business_factory.create()
    business_b = await business_factory.create()
    offering_id = await _seed_offering(db_session, business_a)
    repo = SqlOfferingEconomicsRepository(db_session)

    assert await repo.offering_exists(
        business_id=BusinessId(business_a), offering_id=ProductId(offering_id)
    )
    assert not await repo.offering_exists(
        business_id=BusinessId(business_b), offering_id=ProductId(offering_id)
    )


async def test_upsert_then_get_margin_inputs_round_trips(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = await business_factory.create()
    offering_id = await _seed_offering(db_session, business_id)
    typed_business_id = BusinessId(business_id)
    typed_offering_id = ProductId(offering_id)

    await SqlOfferingEconomicsRepository(db_session).upsert(
        business_id=typed_business_id, offering_id=typed_offering_id, economics=_input()
    )

    margin_inputs = await SqlMarginInputsPort(db_session).get_margin_inputs(
        business_id=typed_business_id, product_id=typed_offering_id
    )

    assert margin_inputs is not None
    assert margin_inputs.vat_rate.value == Decimal("0.21")
    assert margin_inputs.delivery_cost.amount == Decimal("90.00")
    assert margin_inputs.monthly_sales_team_cost.amount == Decimal("140.00")
    assert margin_inputs.refund_rate_override is not None
    assert margin_inputs.refund_rate_override.value == Decimal("0.06")


async def test_get_margin_inputs_is_none_without_a_row(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = await business_factory.create()
    offering_id = await _seed_offering(db_session, business_id)

    margin_inputs = await SqlMarginInputsPort(db_session).get_margin_inputs(
        business_id=BusinessId(business_id), product_id=ProductId(offering_id)
    )

    assert margin_inputs is None


async def test_refund_rate_pct_none_falls_back_to_no_override(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = await business_factory.create()
    offering_id = await _seed_offering(db_session, business_id)
    typed_business_id = BusinessId(business_id)
    typed_offering_id = ProductId(offering_id)

    await SqlOfferingEconomicsRepository(db_session).upsert(
        business_id=typed_business_id,
        offering_id=typed_offering_id,
        economics=_input(refund_rate_pct=None),
    )

    margin_inputs = await SqlMarginInputsPort(db_session).get_margin_inputs(
        business_id=typed_business_id, product_id=typed_offering_id
    )

    assert margin_inputs is not None
    assert margin_inputs.refund_rate_override is None


async def test_upsert_is_idempotent_and_updates_in_place(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = await business_factory.create()
    offering_id = await _seed_offering(db_session, business_id)
    typed_business_id = BusinessId(business_id)
    typed_offering_id = ProductId(offering_id)
    repo = SqlOfferingEconomicsRepository(db_session)

    await repo.upsert(
        business_id=typed_business_id, offering_id=typed_offering_id, economics=_input()
    )
    updated = await repo.upsert(
        business_id=typed_business_id,
        offering_id=typed_offering_id,
        economics=_input(vat_rate_pct=Decimal("0")),
    )

    assert updated.vat_rate_pct == Decimal("0")
    count = await db_session.execute(
        text("SELECT count(*) FROM offering_economics WHERE offering_id = :id"),
        {"id": offering_id},
    )
    assert count.scalar_one() == 1


async def test_list_offerings_with_economics_marks_missing_ones_as_none(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = await business_factory.create()
    filled_offering_id = await _seed_offering(db_session, business_id)
    bare_offering_id = await _seed_offering(db_session, business_id)
    typed_business_id = BusinessId(business_id)
    repo = SqlOfferingEconomicsRepository(db_session)
    await repo.upsert(
        business_id=typed_business_id,
        offering_id=ProductId(filled_offering_id),
        economics=_input(),
    )

    offerings = await repo.list_offerings_with_economics(business_id=typed_business_id)

    by_id = {o.offering_id: o for o in offerings}
    assert by_id[str(filled_offering_id)].economics is not None
    assert by_id[str(bare_offering_id)].economics is None
