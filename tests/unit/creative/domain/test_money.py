"""`Money` local a `creative` (ver `src/safent_ads/creative/domain/money.py`
sobre por que no vive todavia en `shared`)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from safent_ads.creative.domain.money import CurrencyMismatchError, Money


def test_rejects_non_iso4217_currency() -> None:
    with pytest.raises(ValueError, match="divisa invalida"):
        Money(Decimal("1"), "eur")


def test_add_same_currency() -> None:
    total = Money(Decimal("1.50"), "EUR") + Money(Decimal("2.50"), "EUR")

    assert total == Money(Decimal("4.00"), "EUR")


def test_add_different_currency_raises() -> None:
    with pytest.raises(CurrencyMismatchError):
        Money(Decimal("1"), "EUR") + Money(Decimal("1"), "USD")


def test_comparison_different_currency_raises() -> None:
    with pytest.raises(CurrencyMismatchError):
        _ = Money(Decimal("1"), "EUR") > Money(Decimal("1"), "USD")


def test_zero() -> None:
    assert Money.zero("EUR") == Money(Decimal("0"), "EUR")
