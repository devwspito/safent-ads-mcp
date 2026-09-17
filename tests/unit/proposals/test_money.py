"""`Money` — VO local a `proposals` (ver docstring de `money.py`)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from safent_ads.proposals.domain.money import CurrencyMismatchError, Money


class TestArithmetic:
    def test_add_same_currency(self) -> None:
        assert Money.of("10.50") + Money.of("5.25") == Money.of("15.75")

    def test_sub_same_currency(self) -> None:
        assert Money.of("10") - Money.of("3") == Money.of("7")

    def test_neg(self) -> None:
        assert -Money.of("10") == Money.of("-10")

    def test_add_different_currency_raises(self) -> None:
        with pytest.raises(CurrencyMismatchError):
            Money.of("10", "EUR") + Money.of("10", "USD")

    def test_ordering(self) -> None:
        assert Money.of("5") < Money.of("10")
        assert Money.of("10") >= Money.of("10")


class TestScaledBy:
    def test_scaled_by_rounds_half_up_to_cents(self) -> None:
        result = Money.of("100").scaled_by(Decimal("0.7"))

        assert result == Money.of("70.00")

    def test_scaled_by_fraction_rounds_correctly(self) -> None:
        result = Money.of("10").scaled_by(Decimal("0.125"))

        assert result == Money.of("1.25")


class TestValidation:
    def test_lowercase_currency_rejected(self) -> None:
        with pytest.raises(ValueError, match="ISO-4217"):
            Money(Decimal("1"), "eur")

    def test_zero_is_not_positive(self) -> None:
        assert Money.zero().is_positive() is False

    def test_to_canonical_uses_string_amount(self) -> None:
        assert Money.of("10.50").to_canonical() == {"amount": "10.50", "currency": "EUR"}
