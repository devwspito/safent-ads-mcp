"""`Customer` (spec 027, data-model.md §Customer): prohibido el dato
personal, `first_paid_conversion_at` se deriva, `churned` no borra nada."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.crm.domain.customer import Customer, CustomerState
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.domain.lead_attribution import AttributionRung
from safent_ads.shared.ids import BusinessId

_SEEN_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _identity(business_id: BusinessId) -> HashedIdentity:
    return HashedIdentity.compute(business_id=business_id, raw_identifier="a@example.com", salt="s")


def _new_customer(business_id: BusinessId | None = None) -> Customer:
    business_id = business_id or BusinessId.new()
    return Customer.first_seen(
        business_id=business_id,
        hashed_identity=_identity(business_id),
        entity_ref=None,
        attribution_rung=AttributionRung.AGGREGATE,
        currency="EUR",
        seen_at=_SEEN_AT,
    )


def test_first_seen_starts_as_lead_without_paid_conversion() -> None:
    customer = _new_customer()

    assert customer.state is CustomerState.LEAD
    assert customer.first_paid_conversion_at is None
    assert customer.first_seen_at == customer.last_seen_at == _SEEN_AT


def test_hashed_identity_from_another_business_is_rejected() -> None:
    business_id = BusinessId.new()
    other_business_identity = _identity(BusinessId.new())

    with pytest.raises(ValueError, match="otro negocio"):
        Customer(
            customer_id=_new_customer(business_id).customer_id,
            business_id=business_id,
            hashed_identity=other_business_identity,
            entity_ref=None,
            attribution_rung=AttributionRung.AGGREGATE,
            first_paid_conversion_at=None,
            state=CustomerState.LEAD,
            currency="EUR",
            first_seen_at=_SEEN_AT,
            last_seen_at=_SEEN_AT,
        )


def test_record_paid_event_activates_and_sets_first_paid_conversion_at() -> None:
    customer = _new_customer()
    paid_at = datetime(2026, 2, 1, tzinfo=UTC)

    activated = customer.record_paid_event(occurred_at=paid_at)

    assert activated.state is CustomerState.ACTIVE
    assert activated.first_paid_conversion_at == paid_at
    assert activated.last_seen_at == paid_at


def test_first_paid_conversion_at_derives_as_the_minimum_occurred_at() -> None:
    customer = _new_customer()
    later = customer.record_paid_event(occurred_at=datetime(2026, 3, 1, tzinfo=UTC))
    earlier = later.record_paid_event(occurred_at=datetime(2026, 1, 15, tzinfo=UTC))

    assert earlier.first_paid_conversion_at == datetime(2026, 1, 15, tzinfo=UTC)


def test_churn_does_not_erase_first_paid_conversion_at() -> None:
    customer = _new_customer().record_paid_event(occurred_at=datetime(2026, 2, 1, tzinfo=UTC))

    churned = customer.record_churn(occurred_at=datetime(2026, 4, 1, tzinfo=UTC))

    assert churned.state is CustomerState.CHURNED
    assert churned.first_paid_conversion_at == datetime(2026, 2, 1, tzinfo=UTC)


def test_reactivate_returns_to_active_after_churn() -> None:
    customer = _new_customer().record_churn(occurred_at=datetime(2026, 4, 1, tzinfo=UTC))

    reactivated = customer.reactivate(occurred_at=datetime(2026, 5, 1, tzinfo=UTC))

    assert reactivated.state is CustomerState.ACTIVE


def test_touch_never_moves_last_seen_at_backwards() -> None:
    customer = _new_customer()

    touched = customer.touch(seen_at=datetime(2025, 12, 1, tzinfo=UTC))

    assert touched.last_seen_at == _SEEN_AT
