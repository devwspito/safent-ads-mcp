"""`CustomerValue` (spec 027, data-model.md §CustomerValue): sin volumen o
ventana madura, no hay numero -- nunca un cero fabricado (FR-009 de spec 026)."""

from __future__ import annotations

import pytest

from safent_ads.crm.domain.customer_value import CustomerValue, CustomerValueInvariantError
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()


def test_no_number_requires_a_reason() -> None:
    value = CustomerValue.no_number(
        business_id=_BUSINESS_ID,
        cohort_size=3,
        observed_contribution_minor=15_000,
        currency="EUR",
        maturity=0.1,
        horizon_days=90,
        reason="cohorte de 3 clientes: sin numero",
    )

    assert value.is_provisional is True
    assert value.projected_contribution_minor is None
    assert value.no_number_reason


def test_observed_value_is_never_provisional() -> None:
    value = CustomerValue.observed(
        business_id=_BUSINESS_ID,
        cohort_size=20,
        observed_contribution_minor=1_000_000,
        currency="EUR",
        projected_contribution_minor=1_500_000,
        maturity=0.8,
        horizon_days=90,
    )

    assert value.is_provisional is False
    assert value.no_number_reason is None
    assert value.projected_contribution_minor == 1_500_000


def test_provisional_without_reason_is_an_illegal_state() -> None:
    with pytest.raises(CustomerValueInvariantError):
        CustomerValue(
            business_id=_BUSINESS_ID,
            cohort_size=1,
            observed_contribution_minor=0,
            currency="EUR",
            projected_contribution_minor=None,
            maturity=0.0,
            horizon_days=90,
            is_provisional=True,
            no_number_reason=None,
        )


def test_non_provisional_without_projection_is_an_illegal_state() -> None:
    with pytest.raises(CustomerValueInvariantError):
        CustomerValue(
            business_id=_BUSINESS_ID,
            cohort_size=20,
            observed_contribution_minor=100,
            currency="EUR",
            projected_contribution_minor=None,
            maturity=1.0,
            horizon_days=90,
            is_provisional=False,
            no_number_reason=None,
        )


def test_negative_cohort_size_is_rejected() -> None:
    with pytest.raises(CustomerValueInvariantError):
        CustomerValue.no_number(
            business_id=_BUSINESS_ID,
            cohort_size=-1,
            observed_contribution_minor=0,
            currency="EUR",
            maturity=0.0,
            horizon_days=90,
            reason="motivo",
        )
