"""`canonical_platform_account_id` -- spec 008 T028/T029, `data-model.md`
§`AccountHardCap` "Clave canonica". La comparten `set_account_caps`,
`delete_account_caps`, `resolve_account_caps` y la ruta que aplica una
escritura: si divergieran, el panel guardaria bajo una clave que nadie
consulta."""

from __future__ import annotations

import pytest

from safent_ads.broker.domain.account_key import (
    InvalidPlatformAccountIdError,
    canonical_platform_account_id,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1234567890", "1234567890"),
        ("123-456-7890", "1234567890"),
        ("  123-456-7890  ", "1234567890"),
        ("act_123456", "act_123456"),
        ("ACT_123456", "act_123456"),
        ("Act_123456", "act_123456"),
        (" act_123456 ", "act_123456"),
        ("example_platform_account_id", "example_platform_account_id"),
    ],
)
def test_canonical_form_of_the_two_platform_shapes(raw: str, expected: str) -> None:
    assert canonical_platform_account_id(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "1234567890",
        "123-456-7890",
        "act_123456",
        "ACT_123456",
        "  act_9  ",
        "example_platform_account_id",
    ],
)
def test_canonicalization_is_idempotent(raw: str) -> None:
    """De esto depende que la clave guardada y la consultada coincidan
    siempre, se canonicalice una vez o dos por el camino."""
    once = canonical_platform_account_id(raw)

    assert canonical_platform_account_id(once) == once


def test_an_unknown_shape_is_respected_never_guessed() -> None:
    assert canonical_platform_account_id("cuenta-de-pruebas") == "cuenta-de-pruebas"


@pytest.mark.parametrize(
    "raw", ["", "   ", "a" * 65, "cuenta/otra", "cuenta\\otra", "cuenta\nid", "cuenta\x00id"]
)
def test_shapes_no_real_platform_id_has_are_rejected(raw: str) -> None:
    with pytest.raises(InvalidPlatformAccountIdError):
        canonical_platform_account_id(raw)


def test_a_nine_digit_number_is_not_a_google_customer_id() -> None:
    """Solo 10 digitos exactos, o el formato con guiones. Nada de
    reescribir a ciegas cualquier numero."""
    assert canonical_platform_account_id("123456789") == "123456789"
