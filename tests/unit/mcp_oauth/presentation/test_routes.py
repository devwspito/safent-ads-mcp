"""`build_oauth_routes` (tasks.md T013, contracts/oauth.md SS1,
threat-model.md C-49): las dos rutas de metadatos exactas del contrato,
incluida la ausencia deliberada de `client_id_metadata_document_supported`
(obliga a Codex `--oauth-client-registration auto` a usar DCR), y el
parche de `none`/`authorization_response_iss_parameter_supported` que el
SDK no anuncia por defecto."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from safent_ads.mcp_oauth.presentation import routes as routes_module
from safent_ads.mcp_oauth.presentation.routes import build_oauth_routes

_PUBLIC_BASE_URL = "https://ads.example.com"
_RESOURCE_NAME = "Acme Ads MCP"


class _StubProvider:
    """Nunca se le llama en estas pruebas: solo hace falta como argumento
    posicional de `create_auth_routes` para construir los handlers -- los
    metadatos no dependen de el."""


def _client() -> TestClient:
    routes = build_oauth_routes(
        _StubProvider(), public_base_url=_PUBLIC_BASE_URL, resource_name=_RESOURCE_NAME
    )  # type: ignore[arg-type]
    app = Starlette(routes=routes)
    return TestClient(app, base_url=_PUBLIC_BASE_URL)


def test_authorization_server_metadata_matches_the_contract_exactly() -> None:
    with _client() as client:
        response = client.get("/.well-known/oauth-authorization-server")

    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert body == {
        "issuer": _PUBLIC_BASE_URL,
        "authorization_endpoint": f"{_PUBLIC_BASE_URL}/authorize",
        "token_endpoint": f"{_PUBLIC_BASE_URL}/token",
        "registration_endpoint": f"{_PUBLIC_BASE_URL}/register",
        "revocation_endpoint": f"{_PUBLIC_BASE_URL}/revoke",
        "scopes_supported": ["ads:read", "ads:propose"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        # M2 de la revision de seguridad (16-sep): SOLO `none` --
        # `SdkOAuthProvider._require_public_client()` nunca registra otra
        # cosa que clientes `none`, y `/revoke` los autentica con el mismo
        # `ClientAuthenticator` que `/token` -- anunciar
        # `client_secret_post`/`client_secret_basic` era una promesa falsa
        # que `register_client()` siempre rechaza (encontrado por
        # `tests/contracts/mcp_oauth/test_metadata_documents.py`, corregido
        # en `_patch_authorization_server_metadata_route`).
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "authorization_response_iss_parameter_supported": True,
    }


def test_authorization_server_metadata_never_advertises_client_id_metadata_document() -> None:
    """contracts/oauth.md SS1: "ausente a proposito: obliga a Codex
    `--oauth-client-registration auto` a usar DCR"."""
    with _client() as client:
        response = client.get("/.well-known/oauth-authorization-server")

    assert "client_id_metadata_document_supported" not in response.json()


def test_issuer_has_no_trailing_slash() -> None:
    """RFC 8414: el emisor se compara como cadena EXACTA -- una barra de
    mas rompe el descubrimiento en silencio (plan.md "Ajustes",
    threat-model.md C-49). `AnyHttpUrl` a secas normaliza anadiendo `/` a
    una URL sin path; `build_oauth_routes` debe evitarlo."""
    with _client() as client:
        response = client.get("/.well-known/oauth-authorization-server")

    assert response.json()["issuer"] == _PUBLIC_BASE_URL


def test_protected_resource_metadata_matches_the_contract_exactly() -> None:
    with _client() as client:
        response = client.get("/.well-known/oauth-protected-resource/mcp")

    assert response.status_code == 200
    assert response.json() == {
        "resource": f"{_PUBLIC_BASE_URL}/mcp",
        "authorization_servers": [_PUBLIC_BASE_URL],
        "scopes_supported": ["ads:read"],
        "bearer_methods_supported": ["header"],
        "resource_name": _RESOURCE_NAME,
    }


@pytest.mark.parametrize("path", ["/authorize", "/token", "/register", "/revoke"])
def test_the_four_as_endpoints_are_registered(path: str) -> None:
    routes = build_oauth_routes(
        _StubProvider(), public_base_url=_PUBLIC_BASE_URL, resource_name=_RESOURCE_NAME
    )  # type: ignore[arg-type]

    assert any(route.path == path for route in routes)


def test_missing_metadata_route_fails_loud_instead_of_serving_it_unpatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I3 de la revision de seguridad (16-sep): si `create_auth_routes` del
    SDK dejara de registrar `/.well-known/oauth-authorization-server`, C-49
    (`none` + `authorization_response_iss_parameter_supported`) dejaria de
    aplicarse EN SILENCIO -- debe fallar alto en su lugar."""
    monkeypatch.setattr(routes_module, "create_auth_routes", lambda *_args, **_kwargs: [])

    with pytest.raises(RuntimeError, match="oauth-authorization-server"):
        build_oauth_routes(
            _StubProvider(), public_base_url=_PUBLIC_BASE_URL, resource_name=_RESOURCE_NAME
        )  # type: ignore[arg-type]
