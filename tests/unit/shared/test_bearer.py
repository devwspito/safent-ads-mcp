"""`extract_bearer_token`/`is_token_valid` (shared.bearer): comparacion en
tiempo constante compartida por `/mcp` y `/mcp/health`."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from safent_ads.shared.bearer import extract_bearer_token, is_token_valid


def test_extract_bearer_token_returns_none_without_prefix() -> None:
    assert extract_bearer_token("token-abc") is None


def test_extract_bearer_token_returns_none_for_empty_header() -> None:
    assert extract_bearer_token("") is None


def test_extract_bearer_token_strips_prefix() -> None:
    assert extract_bearer_token("Bearer abc123") == "abc123"


def test_extract_bearer_token_returns_none_for_non_bearer_scheme() -> None:
    assert extract_bearer_token("Basic abc123") is None


def test_extract_bearer_token_returns_none_when_scheme_has_no_token() -> None:
    assert extract_bearer_token("Bearer ") is None
    assert extract_bearer_token("Bearer") is None


def test_extract_bearer_token_is_case_insensitive_on_the_scheme() -> None:
    """M1 de la revision de seguridad (16-sep): el SDK ya acepta
    `bearer`/`BEARER` en `/mcp` (`BearerAuthBackend.authenticate`,
    `.lower().startswith("bearer ")`) -- `GET /mcp/health` tiene que
    aceptar exactamente lo mismo, o un cliente que manda el esquema en
    minuscula pasaria en uno y no en el otro."""
    assert extract_bearer_token("bearer abc123") == "abc123"
    assert extract_bearer_token("BEARER abc123") == "abc123"
    assert extract_bearer_token("BeArEr abc123") == "abc123"


def test_is_token_valid_accepts_matching_token() -> None:
    assert is_token_valid("secret-token", "secret-token") is True


def test_is_token_valid_rejects_mismatched_token() -> None:
    assert is_token_valid("wrong-token", "secret-token") is False


def test_is_token_valid_delegates_to_constant_time_comparison() -> None:
    """Regresion: si esto vuelve a ser un `==`, el test lo detecta sin
    depender de medir tiempos (que en CI es tan fiable como parece)."""
    with patch("safent_ads.shared.bearer.hmac.compare_digest", return_value=True) as spy:
        assert is_token_valid("abc", "xyz") is True

    spy.assert_called_once_with(b"abc", b"xyz")


@pytest.mark.parametrize(
    "presentado",
    [
        pytest.param("ñ", id="latin-1"),
        pytest.param("токен", id="cirilico"),
        pytest.param("\U0001f600", id="fuera-del-plano-basico"),
        pytest.param("\ud800", id="sustituto-suelto"),
    ],
)
def test_is_token_valid_rejects_a_non_ascii_token_instead_of_exploding(presentado: str) -> None:
    """`hmac.compare_digest` sobre `str` exige ASCII puro y, si no, lanza
    `TypeError`. El token llega de una cabecera HTTP, o sea de fuera: un
    `Authorization: Bearer ñ` convertia una credencial invalida -- un 401 de
    manual -- en un 500. Y un 500 ademas distingue: le dice a quien prueba
    que ese byte llego mas lejos que los otros."""
    assert is_token_valid(presentado, "secret-token") is False


def test_is_token_valid_accepts_a_non_ascii_token_that_matches() -> None:
    """La contraparte: comparar en bytes no puede romper el caso bueno."""
    assert is_token_valid("contraseña-ñ", "contraseña-ñ") is True
