"""`RevenueEvent` (spec 027, data-model.md §RevenueEvent): solo-anexable,
`refund`/`churn` restan con importe negativo, nunca reescriben historia."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.crm.domain.customer import CustomerId
from safent_ads.crm.domain.errors import (
    ChurnMustNotBePositiveError,
    NonPositiveRevenueAmountError,
    RefundMustBeNegativeError,
    UnsupportedCurrencyError,
)
from safent_ads.crm.domain.revenue_event import RevenueEvent, RevenueEventId, RevenueEventKind

_OCCURRED_AT = datetime(2026, 3, 1, tzinfo=UTC)


def _build(kind: RevenueEventKind, amount_minor: int) -> RevenueEvent:
    return RevenueEvent(
        revenue_event_id=RevenueEventId.new(),
        customer_id=CustomerId.new(),
        kind=kind,
        amount_minor=amount_minor,
        currency="EUR",
        occurred_at=_OCCURRED_AT,
        observed_at=_OCCURRED_AT,
        source_event_id="evt-1",
        mapping_version=1,
    )


def test_first_payment_requires_positive_amount() -> None:
    with pytest.raises(NonPositiveRevenueAmountError):
        _build(RevenueEventKind.FIRST_PAYMENT, 0)


def test_refund_requires_negative_amount() -> None:
    with pytest.raises(RefundMustBeNegativeError):
        _build(RevenueEventKind.REFUND, 100)


def test_refund_with_negative_amount_is_valid_and_is_a_reduction() -> None:
    event = _build(RevenueEventKind.REFUND, -500)

    assert event.is_reduction is True
    assert event.amount_minor == -500


def test_churn_rejects_positive_amount() -> None:
    with pytest.raises(ChurnMustNotBePositiveError):
        _build(RevenueEventKind.CHURN, 1)


def test_churn_allows_zero() -> None:
    event = _build(RevenueEventKind.CHURN, 0)

    assert event.is_reduction is True


def test_recurring_payment_is_not_a_reduction() -> None:
    event = _build(RevenueEventKind.RECURRING_PAYMENT, 4900)

    assert event.is_reduction is False


def test_currency_must_be_iso4217_uppercase() -> None:
    with pytest.raises(UnsupportedCurrencyError):
        _build(RevenueEventKind.FIRST_PAYMENT, 100).__class__(
            revenue_event_id=RevenueEventId.new(),
            customer_id=CustomerId.new(),
            kind=RevenueEventKind.FIRST_PAYMENT,
            amount_minor=100,
            currency="eur",
            occurred_at=_OCCURRED_AT,
            observed_at=_OCCURRED_AT,
            source_event_id="evt-2",
            mapping_version=1,
        )


def test_blank_source_event_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="source_event_id"):
        RevenueEvent(
            revenue_event_id=RevenueEventId.new(),
            customer_id=CustomerId.new(),
            kind=RevenueEventKind.FIRST_PAYMENT,
            amount_minor=100,
            currency="EUR",
            occurred_at=_OCCURRED_AT,
            observed_at=_OCCURRED_AT,
            source_event_id="  ",
            mapping_version=1,
        )
