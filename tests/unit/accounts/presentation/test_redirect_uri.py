"""Regresion: `platform_app_redirect_uri` dentro de Safent (companion mode)
tiene que derivar el origen del navegador (los `X-Forwarded-*` que añade el
puente `ads_bridge`, lumen-runtime-next), no `settings.public_base_url`
(`https://ads.safent.internal:8443`, host que la webview no puede resolver
y que Google/Meta no aceptan registrar) -- bug real: el OAuth "Conectar" no
podia completarse dentro de la app empaquetada."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import Request

from safent_ads.accounts.presentation.redirect_uri import platform_app_redirect_uri
from safent_ads.composition.settings import ApiSettings
from safent_ads.shared.ids import PlatformCode
from tests.unit.composition.factories import build_api_settings

_CALLBACK_SUFFIX = "/api/v1/platform-accounts/google/reconnect/callback"


def _request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
    }
    return Request(scope)


@pytest.fixture
def companion_settings(tmp_path: Path) -> ApiSettings:
    certfile = tmp_path / "leaf.crt"
    certfile.write_text("cert")
    keyfile = tmp_path / "leaf.key"
    keyfile.write_text("key")
    return build_api_settings(companion_mode=True, tls_certfile=certfile, tls_keyfile=keyfile)


def test_without_request_falls_back_to_static_public_base_url() -> None:
    settings = build_api_settings()

    uri = platform_app_redirect_uri(settings, PlatformCode.GOOGLE)

    assert uri == f"{settings.public_base_url}{_CALLBACK_SUFFIX}"


def test_companion_mode_derives_origin_from_forwarded_headers(
    companion_settings: ApiSettings,
) -> None:
    request = _request(
        {
            "x-forwarded-host": "127.0.0.1:54213",
            "x-forwarded-proto": "http",
            "x-forwarded-prefix": "/ads",
        }
    )

    uri = platform_app_redirect_uri(companion_settings, PlatformCode.GOOGLE, request)

    assert uri == f"http://127.0.0.1:54213/ads{_CALLBACK_SUFFIX}"


def test_forwarded_prefix_is_joined_without_double_slash(
    companion_settings: ApiSettings,
) -> None:
    request = _request(
        {
            "x-forwarded-host": "127.0.0.1:9999",
            "x-forwarded-proto": "http",
            "x-forwarded-prefix": "/ads",
        }
    )

    uri = platform_app_redirect_uri(companion_settings, PlatformCode.GOOGLE, request)

    assert "//api" not in uri.split("://", 1)[1]
    assert uri == f"http://127.0.0.1:9999/ads{_CALLBACK_SUFFIX}"


def test_missing_forwarded_proto_defaults_to_https(companion_settings: ApiSettings) -> None:
    request = _request({"x-forwarded-host": "127.0.0.1:9999"})

    uri = platform_app_redirect_uri(companion_settings, PlatformCode.GOOGLE, request)

    assert uri.startswith("https://127.0.0.1:9999")


def test_untrusted_forwarded_prefix_is_dropped_not_propagated(
    companion_settings: ApiSettings,
) -> None:
    request = _request(
        {"x-forwarded-host": "127.0.0.1:9999", "x-forwarded-prefix": "//evil.example.com"}
    )

    uri = platform_app_redirect_uri(companion_settings, PlatformCode.GOOGLE, request)

    assert uri == f"https://127.0.0.1:9999{_CALLBACK_SUFFIX}"


def test_forwarded_headers_ignored_outside_companion_mode() -> None:
    """El origen forjado por un cliente cualquiera nunca debe ganarle a
    `public_base_url` fuera de modo companion -- `ads-api` no esta detras
    del puente en ese caso (compose.yaml publica 127.0.0.1:8410 directo),
    asi que confiar en estas cabeceras seria un vector host-header."""
    settings = build_api_settings(companion_mode=False)
    request = _request(
        {
            "x-forwarded-host": "evil.example.com",
            "x-forwarded-proto": "http",
            "x-forwarded-prefix": "/ads",
        }
    )

    uri = platform_app_redirect_uri(settings, PlatformCode.GOOGLE, request)

    assert uri == f"{settings.public_base_url}{_CALLBACK_SUFFIX}"


def test_request_without_forwarded_host_falls_back_even_in_companion_mode(
    companion_settings: ApiSettings,
) -> None:
    request = _request({})

    uri = platform_app_redirect_uri(companion_settings, PlatformCode.GOOGLE, request)

    assert uri == f"{companion_settings.public_base_url}{_CALLBACK_SUFFIX}"


def test_derivation_is_deterministic_for_authorize_and_exchange(
    companion_settings: ApiSettings,
) -> None:
    """`connections_router.py::start` computa `redirect_uri` una sola vez y
    el broker lo persiste junto al `state` (`OAuthConnectFlow`) para
    reusarlo byte a byte en el canje -- esto fija que dos llamadas con la
    misma peticion nunca puedan divergir (lo que si se pasara `request`
    distinto en cada paso, romperia)."""
    request = _request({"x-forwarded-host": "127.0.0.1:54213", "x-forwarded-prefix": "/ads"})

    first = platform_app_redirect_uri(companion_settings, PlatformCode.META, request)
    second = platform_app_redirect_uri(companion_settings, PlatformCode.META, request)

    assert first == second
