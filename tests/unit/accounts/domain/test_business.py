"""Invariantes de `Business` (data-model.md)."""

from __future__ import annotations

import pytest

from safent_ads.accounts.domain.business import Business, InvalidBusinessError
from safent_ads.shared.ids import BusinessId


def _business(**overrides: object) -> Business:
    defaults: dict[str, object] = {
        "business_id": BusinessId.new(),
        "name": "Negocio Ejemplo",
        "slug": "negocio-ejemplo",
        "timezone": "Europe/Madrid",
        "reference_currency": "EUR",
    }
    defaults.update(overrides)
    return Business(**defaults)  # type: ignore[arg-type]


def test_valid_business_is_active_by_default() -> None:
    business = _business()

    assert business.is_active is True


@pytest.mark.parametrize("slug", ["Negocio", "negocio_uno", "-negocio", "negocio-", ""])
def test_invalid_slug_raises(slug: str) -> None:
    with pytest.raises(InvalidBusinessError):
        _business(slug=slug)


def test_empty_name_raises() -> None:
    with pytest.raises(InvalidBusinessError):
        _business(name="   ")


def test_invalid_currency_raises() -> None:
    with pytest.raises(InvalidBusinessError):
        _business(reference_currency="eur")


def test_deactivate_and_activate() -> None:
    business = _business()

    business.deactivate()
    assert business.is_active is False

    business.activate()
    assert business.is_active is True
