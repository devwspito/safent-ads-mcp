"""`Money` es exacto (minor units) y no mezcla divisas."""

from __future__ import annotations

import pytest

from safent_ads.accounts.domain.money import CurrencyMismatchError, InvalidMoneyError, Money


def test_add_same_currency() -> None:
    total = Money(1000, "EUR") + Money(250, "EUR")

    assert total == Money(1250, "EUR")


def test_add_different_currency_raises() -> None:
    with pytest.raises(CurrencyMismatchError):
        Money(1000, "EUR") + Money(1000, "USD")


def test_negative_minor_units_raises() -> None:
    with pytest.raises(InvalidMoneyError):
        Money(-1, "EUR")


@pytest.mark.parametrize("currency", ["eur", "EU", "EURO", ""])
def test_invalid_currency_code_raises(currency: str) -> None:
    with pytest.raises(InvalidMoneyError):
        Money(100, currency)


def test_ordering_same_currency() -> None:
    assert Money(100, "EUR") < Money(200, "EUR")
    assert Money(100, "EUR") <= Money(100, "EUR")
