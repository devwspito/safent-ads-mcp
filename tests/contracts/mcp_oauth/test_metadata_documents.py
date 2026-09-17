"""Contrato exacto de los dos documentos de descubrimiento OAuth
(`contracts/oauth.md` SS1, threat-model.md C-49):
`/.well-known/oauth-authorization-server` y
`/.well-known/oauth-protected-resource/mcp`.

Comparacion de igualdad de DICCIONARIO completo, no solo de claves sueltas:
si algun campo nuevo se colase (o uno documentado desapareciese) el test
falla, sin tener que enumerar aparte cada ausencia esperada
(`client_id_metadata_document_supported` incluido, RFC 8414: el SDK 2.2 no
lo declara y el AS no lo activa a proposito, para forzar DCR en
Codex `--oauth-client-registration auto`).

Sin Postgres: ninguna de las dos rutas toca la base de datos (tasks.md
T019, mismo patron sin DB que `tests/unit/test_api_hardening.py`)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from safent_ads.composition.app import create_app
from safent_ads.composition.settings import ApiSettings

_PUBLIC_BASE_URL = "https://ads.test.ts.net"


def test_authorization_server_metadata_matches_contract_exactly(
    api_settings: ApiSettings,
) -> None:
    app = create_app(api_settings)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/.well-known/oauth-authorization-server")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "issuer": _PUBLIC_BASE_URL,
        "authorization_endpoint": f"{_PUBLIC_BASE_URL}/authorize",
        "token_endpoint": f"{_PUBLIC_BASE_URL}/token",
        "registration_endpoint": f"{_PUBLIC_BASE_URL}/register",
        "revocation_endpoint": f"{_PUBLIC_BASE_URL}/revoke",
        "scopes_supported": ["ads:read", "ads:propose"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        # M2 de la revision de seguridad (16-sep): SOLO "none" en ambas
        # listas -- `SdkOAuthProvider._require_public_client()` NUNCA
        # registra otra cosa que clientes "none" (RFC 8252 SS8.4), y
        # `ClientAuthenticator.authenticate_request()` (el mismo que usa
        # `/revoke`) acepta un cliente "none" sin secreto -- confirmado
        # abajo por `test_public_client_can_call_revoke_without_a_client_secret`.
        # Anunciar "client_secret_post"/"client_secret_basic" era una
        # promesa falsa que `register_client()` siempre rechaza.
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "authorization_response_iss_parameter_supported": True,
    }


def test_protected_resource_metadata_matches_contract_exactly(
    api_settings: ApiSettings,
) -> None:
    app = create_app(api_settings)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/.well-known/oauth-protected-resource/mcp")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "resource": f"{_PUBLIC_BASE_URL}/mcp",
        "authorization_servers": [_PUBLIC_BASE_URL],
        "scopes_supported": ["ads:read"],
        "bearer_methods_supported": ["header"],
        "resource_name": "Ads MCP",
    }


def test_bare_protected_resource_metadata_matches_the_per_resource_document(
    api_settings: ApiSettings,
) -> None:
    """T049 (spec 008): varios clientes (Codex incluido) prueban la ruta
    PELADA de RFC 9728 antes que la "por recurso" -- sin ruta propia caia
    en el catch-all de SPA y devolvia 200 con el HTML del panel en vez de
    JSON. Debe responder EXACTAMENTE lo mismo que `/mcp` (mismo documento,
    unico recurso que este AS protege)."""
    app = create_app(api_settings)

    with TestClient(app, base_url="https://testserver") as client:
        bare = client.get("/.well-known/oauth-protected-resource")
        per_resource = client.get("/.well-known/oauth-protected-resource/mcp")

    assert bare.status_code == 200
    assert bare.headers["content-type"].startswith("application/json")
    assert bare.json() == per_resource.json()


def test_unsupported_well_known_document_is_a_clean_404_never_the_panel_shell(
    api_settings: ApiSettings,
) -> None:
    """T049 (spec 008): este AS no es un proveedor OIDC (RFC 8414 SS3 es un
    protocolo distinto de RFC 9728/RFC 8414 SS2 que si implementamos) --
    `/.well-known/openid-configuration` (o cualquier otro documento que no
    publicamos) debe dar 404 JSON, nunca el 200 HTML del panel que serviria
    el catch-all de SPA sin esta ruta."""
    app = create_app(api_settings)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/.well-known/openid-configuration")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_metadata_routes_are_absent_when_oauth_is_disabled(
    api_settings: ApiSettings, tmp_path: Path
) -> None:
    """threat-model.md C-53/plan.md "Ajustes": `ADS_MCP_OAUTH_ENABLED=false`
    quita el AS entero, no solo `/authorize`/`/token` -- un cliente que
    intente descubrirlo no debe encontrar ninguna huella.

    `panel_dist_dir` se fija a un directorio que NO existe: si el panel
    esta compilado en el arbol de trabajo (`panel/dist/`, p. ej. tras
    `npm run build`), `_mount_panel_spa` monta su catch-all y un 404 real
    se convertiria en 200 con `index.html` -- el resultado de esta prueba
    no puede depender de si alguien compilo el panel antes de correrla."""
    disabled_settings = api_settings.model_copy(
        update={"mcp_oauth_enabled": False, "panel_dist_dir": tmp_path / "no-panel-build"}
    )
    app = create_app(disabled_settings)

    with TestClient(app, base_url="https://testserver") as client:
        as_metadata = client.get("/.well-known/oauth-authorization-server")
        prm_metadata = client.get("/.well-known/oauth-protected-resource/mcp")
        bare_prm_metadata = client.get("/.well-known/oauth-protected-resource")
        unsupported = client.get("/.well-known/openid-configuration")

    assert as_metadata.status_code == 404
    assert prm_metadata.status_code == 404
    assert bare_prm_metadata.status_code == 404
    assert unsupported.status_code == 404
    # Revision de seguridad (PR 44): `well_known_not_found_route()` se
    # registra SIEMPRE (`_register_mcp_oauth_surface`), tambien con OAuth
    # apagado -- los cuatro 404 son el JSON del catch-all, no el texto
    # plano por defecto de Starlette para una ruta que no existe.
    for response in (as_metadata, prm_metadata, bare_prm_metadata, unsupported):
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["error"]["code"] == "NOT_FOUND"
