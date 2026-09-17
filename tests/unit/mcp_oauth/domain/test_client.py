"""`OAuthClient._validate_client_name` (threat-model.md C-70 pieza 3): el
nombre que el cliente elige en el registro dinamico se PINTA entero en la
pantalla de consentimiento, junto al `client_id` y al destino. Tras D-11
es la ultima palanca viva de R-6 (phishing del consentimiento de un
clic): si el nombre puede darle la vuelta al texto o esconder trozos, la
pantalla deja de decir la verdad aunque los datos sean correctos."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.mcp_oauth.domain.client import (
    OAuthClient,
    RedirectUri,
    TokenEndpointAuthMethod,
)
from safent_ads.mcp_oauth.domain.errors import InvalidClientNameError
from safent_ads.mcp_oauth.domain.scope import ScopeSet

_NOW = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def _client(client_name: str) -> OAuthClient:
    return OAuthClient(
        client_id="client-1",
        client_name=client_name,
        redirect_uris=(RedirectUri("http://127.0.0.1:54321/callback"),),
        token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
        client_secret_hash=None,
        grant_types=("authorization_code", "refresh_token"),
        requested_scope=ScopeSet.parse("ads:read"),
        created_at=_NOW,
    )


# Puntos de codigo escritos como `chr(...)`: en el fuente no debe haber ni
# un caracter invisible (es justo lo que esta prueba dice que es peligroso).
_BIDI_OVERRIDE = chr(0x202E)  # RIGHT-TO-LEFT OVERRIDE
_BIDI_ISOLATE_START, _BIDI_ISOLATE_END = chr(0x2066), chr(0x2069)
_ZERO_WIDTH_SPACE = chr(0x200B)
_RIGHT_TO_LEFT_MARK = chr(0x200F)
_BYTE_ORDER_MARK = chr(0xFEFF)
# Los dos que NO se prohiben: ortografia obligatoria en persa, hindi o
# malayalam, y el pegamento de las secuencias de emoji.
_ZERO_WIDTH_NON_JOINER = chr(0x200C)
_ZERO_WIDTH_JOINER = chr(0x200D)


@pytest.mark.parametrize(
    "client_name",
    (
        pytest.param("Claude\x01Code", id="C0"),
        pytest.param("Claude Code\r\nads:aprobar", id="CR/LF"),
        pytest.param("Claude Code\x7f", id="DEL"),
        pytest.param("Claude\x9fCode", id="C1"),
        pytest.param(f"Claude {_BIDI_OVERRIDE}edoC edualC", id="anulacion bidireccional"),
        pytest.param(
            f"Claude {_BIDI_ISOLATE_START}Code{_BIDI_ISOLATE_END}",
            id="aislamiento bidireccional",
        ),
        pytest.param(f"Cla{_ZERO_WIDTH_SPACE}ude Code", id="anchura cero"),
        pytest.param(f"Claude{_RIGHT_TO_LEFT_MARK}Code", id="marca de direccion"),
        pytest.param(f"{_BYTE_ORDER_MARK}Claude Code", id="BOM"),
    ),
)
def test_client_name_rejects_control_and_bidi_characters(client_name: str) -> None:
    with pytest.raises(InvalidClientNameError):
        _client(client_name)


@pytest.mark.parametrize(
    "client_name",
    ("Claude Code", "Códex de Álvaro", "安全 Ads", "Agente (pruebas) — 2026"),
)
def test_a_legible_name_is_accepted(client_name: str) -> None:
    assert _client(client_name).client_name == client_name


@pytest.mark.parametrize(
    "client_name",
    (
        # persa: el ZWNJ separa "narm" de "afzar" en نرم‌افزار (software).
        # Sin el, la palabra se escribe MAL, no solo distinto.
        pytest.param(f"نرم{_ZERO_WIDTH_NON_JOINER}افزار", id="persa con ZWNJ"),
        pytest.param("कोड सहायक", id="devanagari"),
        # `chr(0x1F468) + ZWJ + chr(0x1F4BB)` es un solo emoji compuesto.
        pytest.param(
            f"Agente {chr(0x1F468)}{_ZERO_WIDTH_JOINER}{chr(0x1F4BB)}", id="emoji con ZWJ"
        ),
    ),
)
def test_zero_width_joiners_are_orthography_not_an_attack(client_name: str) -> None:
    """`U+200C`/`U+200D` quedan FUERA de la lista prohibida a proposito:
    no cambian el orden ni esconden texto, y sin ellos media Asia no
    puede escribir el nombre de su propio agente (revision de codigo,
    17-sep). Lo que engana sigue prohibido, un test mas abajo."""
    assert _client(client_name).client_name == client_name


def test_an_empty_name_is_rejected() -> None:
    with pytest.raises(InvalidClientNameError):
        _client("   ")


def test_a_name_longer_than_the_database_check_is_rejected() -> None:
    """Mismo tope que `oauth_clients_client_name_check` (0035_mcp_oauth):
    el dominio no puede admitir lo que la BD despues rechaza con un
    `IntegrityError` opaco."""
    with pytest.raises(InvalidClientNameError):
        _client("a" * 101)
