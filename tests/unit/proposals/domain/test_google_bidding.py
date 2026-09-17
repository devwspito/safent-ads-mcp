"""Google bidding as tagged value objects (data-model.md §Pujas,
tasks.md T011). `target_roas` is a ratio and never
touches `Money` (INV-17)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from safent_ads.proposals.domain.google_bidding import (
    GoogleBiddingError,
    ManualCpc,
    MaximizeClicks,
    MaximizeConversions,
    MaximizeConversionValue,
)
from safent_ads.proposals.domain.money import Money


def test_target_roas_rechaza_amount_currency() -> None:
    with pytest.raises(GoogleBiddingError):
        MaximizeConversionValue(target_roas={"amount": "2.00", "currency": "EUR"})  # type: ignore[arg-type]

    with pytest.raises(GoogleBiddingError):
        MaximizeConversionValue(target_roas=Money.of("2.00"))  # type: ignore[arg-type]


def test_target_cpa_no_positivo_falla() -> None:
    with pytest.raises(GoogleBiddingError):
        MaximizeConversions(target_cpa=Money.of("0"))

    with pytest.raises(GoogleBiddingError):
        MaximizeConversions(target_cpa=Money.of("-1.00"))


def test_cpc_bid_ceiling_no_positivo_falla() -> None:
    with pytest.raises(GoogleBiddingError, match="cpc_bid_ceiling_not_positive"):
        MaximizeClicks(cpc_bid_ceiling=Money.of("0", "EUR"))


def test_cpc_bid_ceiling_positivo_es_valido() -> None:
    assert MaximizeClicks(cpc_bid_ceiling=Money.of("0.50", "EUR")).cpc_bid_ceiling is not None


def test_target_cpa_positivo_es_valido() -> None:
    bidding = MaximizeConversions(target_cpa=Money.of("12.50"))
    assert bidding.target_cpa == Money.of("12.50")


def test_target_cpa_ausente_es_valido() -> None:
    assert MaximizeConversions().target_cpa is None


def test_target_roas_dentro_de_rango_es_valido() -> None:
    bidding = MaximizeConversionValue(target_roas=Decimal("3.5"))
    assert bidding.target_roas == Decimal("3.5")


def test_target_roas_fuera_de_rango_falla() -> None:
    with pytest.raises(GoogleBiddingError):
        MaximizeConversionValue(target_roas=Decimal("0.00"))

    with pytest.raises(GoogleBiddingError):
        MaximizeConversionValue(target_roas=Decimal("100.01"))


def test_target_roas_con_demasiados_decimales_falla() -> None:
    with pytest.raises(GoogleBiddingError):
        MaximizeConversionValue(target_roas=Decimal("1.23456"))


def test_is_conversion_based_por_variante() -> None:
    assert ManualCpc().is_conversion_based is False
    assert MaximizeClicks().is_conversion_based is False
    assert MaximizeConversions().is_conversion_based is True
    assert MaximizeConversionValue().is_conversion_based is True
