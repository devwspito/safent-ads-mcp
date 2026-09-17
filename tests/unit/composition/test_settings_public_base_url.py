"""`ApiSettings.public_base_url` (tasks.md 002 T002, plan.md "Ajustes"):
normaliza la barra final y rechaza path/query/fragment -- RFC 8414 compara
el emisor como cadena exacta, asi que una barra de mas rompe el
descubrimiento del AS en silencio."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.unit.composition.factories import build_api_settings


def test_accepts_https_without_trailing_slash() -> None:
    settings = build_api_settings(public_base_url="https://ads.example.com")

    assert settings.public_base_url == "https://ads.example.com"


def test_strips_a_single_trailing_slash() -> None:
    settings = build_api_settings(public_base_url="https://ads.example.com/")

    assert settings.public_base_url == "https://ads.example.com"


def test_accepts_http_localhost_for_dev() -> None:
    settings = build_api_settings(public_base_url="http://localhost:8410")

    assert settings.public_base_url == "http://localhost:8410"


def test_accepts_http_127_0_0_1_for_dev() -> None:
    """T049 (spec 008): el README documenta la API en `127.0.0.1:8410` --
    antes de este fix, solo `http://localhost` pasaba este validador."""
    settings = build_api_settings(public_base_url="http://127.0.0.1:8410")

    assert settings.public_base_url == "http://127.0.0.1:8410"


def test_accepts_http_ipv6_loopback_for_dev() -> None:
    settings = build_api_settings(public_base_url="http://[::1]:8410")

    assert settings.public_base_url == "http://[::1]:8410"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTP://localhost:8410", "http://localhost:8410"),
        ("http://LOCALHOST:8410", "http://localhost:8410"),
        ("HTTPS://Ads.Example.com", "https://ads.example.com"),
        ("https://Ads.Example.com:9443", "https://ads.example.com:9443"),
        ("HTTP://[::1]:8410", "http://[::1]:8410"),
        ("http://[::1]", "http://[::1]"),
    ],
)
def test_normalizes_scheme_and_host_to_lowercase(raw: str, expected: str) -> None:
    """Revision de PR 44 (T049): `urlsplit` ya trata `scheme`/`hostname`
    sin distinguir mayusculas al DECIDIR si la URL se admite, pero sin
    esta normalizacion el valor GUARDADO conservaba las mayusculas
    originales -- `ResourceIndicator.canonical()` las arrastraba hasta el
    `resource` que Postgres compara con un `~` case-sensitive
    (0054_mcp_oauth_loopback_resource), y el primer `/authorize` volvia a
    reventar con un `IntegrityError` opaco."""
    settings = build_api_settings(public_base_url=raw)

    assert settings.public_base_url == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://ads.example.com:443", "https://ads.example.com"),
        ("http://localhost:80", "http://localhost"),
        ("HTTPS://Ads.Example.com:443", "https://ads.example.com"),
        # El puerto por defecto del OTRO esquema no se toca: `:443` no es
        # el puerto por defecto de `http`, `:80` no lo es de `https`.
        ("http://localhost:443", "http://localhost:443"),
        ("https://ads.example.com:80", "https://ads.example.com:80"),
        # Un puerto explicito que no es el por defecto se conserva tal cual.
        ("https://ads.example.com:9443", "https://ads.example.com:9443"),
    ],
)
def test_strips_the_schemes_default_port(raw: str, expected: str) -> None:
    """Revision de seguridad (PR 44, MINOR a): el SDK anuncia `issuer`/
    `resource` como `pydantic.AnyHttpUrl`, que SIEMPRE serializa sin un
    puerto por defecto explicito (WHATWG URL) -- `ResourceIndicator.
    canonical()` en cambio concatena `public_base_url` tal cual, sin pasar
    por `AnyHttpUrl`. Sin esta normalizacion, `ADS_PUBLIC_BASE_URL=https://
    host:443` haria que los metadatos anunciasen `resource=https://
    host/mcp` mientras el `resource` REALMENTE persistido fuera `https://
    host:443/mcp` -- un cliente que pide exactamente lo que descubrio
    chocaria SIEMPRE con `invalid_target`."""
    settings = build_api_settings(public_base_url=raw)

    assert settings.public_base_url == expected


def test_rejects_http_non_loopback() -> None:
    with pytest.raises(ValidationError, match="https o http de bucle local"):
        build_api_settings(public_base_url="http://ads.example.com")


def test_rejects_a_host_that_only_looks_like_a_loopback_literal() -> None:
    """D-11 (mismo criterio que `mcp_oauth/domain/client.py::RedirectUri`):
    pertenencia EXACTA al conjunto cerrado, nunca un sufijo/prefijo que se
    le parezca."""
    with pytest.raises(ValidationError, match="https o http de bucle local"):
        build_api_settings(public_base_url="http://127.0.0.1.evil.example:8410")


@pytest.mark.parametrize("raw", ["http://localhost:999999", "http://127.0.0.1:65536"])
def test_rejects_a_port_that_does_not_even_parse(raw: str) -> None:
    """Revision de seguridad (PR 44): `urlsplit(...).port` levanta
    `ValueError` para lo que ni siquiera cabe en un puerto -- el mensaje
    debe nombrar `ADS_PUBLIC_BASE_URL`, nunca dejar escapar el `ValueError`
    incidental sin traducir."""
    with pytest.raises(ValidationError, match="ADS_PUBLIC_BASE_URL con puerto invalido"):
        build_api_settings(public_base_url=raw)


def test_rejects_port_zero() -> None:
    """Puerto `0` parsea (a diferencia de `999999`) pero no es un puerto
    donde nadie pueda escuchar -- mismo criterio que `mcp_oauth/domain/
    client.py::RedirectUri`."""
    with pytest.raises(ValidationError, match="ADS_PUBLIC_BASE_URL con puerto invalido"):
        build_api_settings(public_base_url="http://localhost:0")


def test_rejects_a_url_with_no_authority_at_all() -> None:
    """Revision de seguridad (PR 44): `https:` pasa `scheme == "https"`
    (`_is_allowed_public_base_url_origin` no mira si hay autoridad) pero
    `ResourceIndicator.canonical()` -- el mismo criterio que el CHECK de
    Postgres -- SI la exige; la asercion de arranque
    (`_require_a_valid_oauth_resource`) atrapa este caso antes de guardar
    un `public_base_url` que ningun `/authorize` real podria usar."""
    with pytest.raises(ValidationError, match="produce un resource OAuth invalido"):
        build_api_settings(public_base_url="https:")


def test_rejects_a_path() -> None:
    with pytest.raises(ValidationError, match="path/query/fragment"):
        build_api_settings(public_base_url="https://ads.example.com/mcp")


def test_rejects_a_path_hidden_behind_a_trailing_slash() -> None:
    with pytest.raises(ValidationError, match="path/query/fragment"):
        build_api_settings(public_base_url="https://ads.example.com/mcp/")


def test_rejects_a_query_string() -> None:
    with pytest.raises(ValidationError, match="path/query/fragment"):
        build_api_settings(public_base_url="https://ads.example.com?debug=1")


def test_rejects_a_fragment() -> None:
    with pytest.raises(ValidationError, match="path/query/fragment"):
        build_api_settings(public_base_url="https://ads.example.com#section")


def test_rejects_credentials_in_the_url_without_echoing_them() -> None:
    with pytest.raises(ValidationError, match="no admite usuario ni contraseña") as raised:
        build_api_settings(public_base_url="https://operador:clave-muy-secreta@ads.example.com")

    assert "clave-muy-secreta" not in str(raised.value.errors()[0]["msg"])

