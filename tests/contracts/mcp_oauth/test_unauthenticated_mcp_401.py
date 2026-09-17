"""Contrato del 401 del recurso protegido (`contracts/oauth.md` SS2,
threat-model.md C-48): `POST /mcp` y `GET /mcp/health` sin cabecera
`Authorization` devuelven el mismo cuerpo y la misma cabecera
`WWW-Authenticate`, y la URL que esa cabecera apunta (`resource_metadata=`)
responde 200 con el documento RFC 9728 -- un cliente que solo sepa leer el
401 tiene que poder llegar solo, con esa URL, hasta los metadatos.

Sin Postgres: sin cabecera `Authorization`, `SeatCredentialRouter`
(`mcp/presentation/http.py`) responde el 401 sin llegar a resolver ningun
alcance -- nunca abre una sesion de base de datos (mismo patron sin DB que
`tests/integration/mcp/test_streamable_http_endpoint.py`, que solo marca
`integration` porque OTROS casos del mismo fichero si necesitan bearer).

Fusion lane/003: las peticiones a `/mcp` van por el host publico real
(`_PUBLIC_BASE_URL`), no por `testserver` -- `SeatCredentialRouter`
comprueba el `Host` ANTES de la credencial (anti DNS-rebinding, sin gastar
una introspeccion contra Enterprise), asi que un `Host` ajeno daria 403 y
nunca se llegaria a ver el 401 que este contrato fija."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from safent_ads.composition.app import create_app
from safent_ads.composition.settings import ApiSettings

_PUBLIC_BASE_URL = "https://ads.test.ts.net"
_EXPECTED_BODY = {"error": "invalid_token", "error_description": "Authentication required"}
_RESOURCE_METADATA_PATTERN = re.compile(r'resource_metadata="([^"]+)"')


def _resource_metadata_url(www_authenticate: str) -> str:
    match = _RESOURCE_METADATA_PATTERN.search(www_authenticate)
    assert match, f"WWW-Authenticate sin resource_metadata=: {www_authenticate!r}"
    return match.group(1)


def test_post_mcp_without_bearer_is_401_with_resource_metadata(
    api_settings: ApiSettings,
) -> None:
    app = create_app(api_settings)

    with TestClient(app, base_url=_PUBLIC_BASE_URL) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )

    assert response.status_code == 401
    assert response.json() == _EXPECTED_BODY
    www_authenticate = response.headers["www-authenticate"]
    assert www_authenticate.startswith(
        'Bearer error="invalid_token", error_description="Authentication required"'
    )
    resource_metadata_url = _resource_metadata_url(www_authenticate)
    assert resource_metadata_url == (
        "https://ads.test.ts.net/.well-known/oauth-protected-resource/mcp"
    )


def test_get_mcp_health_without_bearer_is_401_with_resource_metadata(
    api_settings: ApiSettings,
) -> None:
    app = create_app(api_settings)

    with TestClient(app, base_url=_PUBLIC_BASE_URL) as client:
        response = client.get("/mcp/health")

    assert response.status_code == 401
    assert response.json() == _EXPECTED_BODY
    resource_metadata_url = _resource_metadata_url(response.headers["www-authenticate"])
    assert resource_metadata_url == (
        "https://ads.test.ts.net/.well-known/oauth-protected-resource/mcp"
    )


def test_the_resource_metadata_url_from_the_401_answers_200_with_the_prm_document(
    api_settings: ApiSettings,
) -> None:
    app = create_app(api_settings)

    with TestClient(app, base_url=_PUBLIC_BASE_URL) as client:
        challenge = client.get("/mcp/health")
        resource_metadata_url = _resource_metadata_url(challenge.headers["www-authenticate"])
        path = resource_metadata_url.removeprefix(_PUBLIC_BASE_URL)

        prm_response = client.get(path)

    assert prm_response.status_code == 200
    body = prm_response.json()
    assert body["resource"] == "https://ads.test.ts.net/mcp"
    assert body["authorization_servers"] == ["https://ads.test.ts.net"]


def test_401_body_and_header_are_identical_for_mcp_and_mcp_health(
    api_settings: ApiSettings,
) -> None:
    """C-48: una sola verificacion de bearer -- ambos endpoints comparten
    literalmente `mcp.presentation.http.unauthorized_response`, no dos
    formatos que puedan divergir con el tiempo."""
    app = create_app(api_settings)

    with TestClient(app, base_url=_PUBLIC_BASE_URL) as client:
        mcp_response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
        health_response = client.get("/mcp/health")

    assert mcp_response.json() == health_response.json()
    assert mcp_response.headers["www-authenticate"] == health_response.headers["www-authenticate"]


def test_wrong_bearer_token_is_also_401_invalid_token(api_settings: ApiSettings) -> None:
    """No solo la ausencia de cabecera: un token que no coincide con el
    estatico recibe la misma forma de error. `mcp_oauth_enabled=False`
    (mismo motivo que `tests/integration/mcp/test_streamable_http_endpoint.py`):
    con OAuth encendido, `CompositeTokenVerifier` intentaria primero una
    consulta real a Postgres antes de caer al estatico, y este fichero no
    trae testcontainer a proposito (contrato, no integracion)."""
    settings_without_oauth = api_settings.model_copy(update={"mcp_oauth_enabled": False})
    app = create_app(settings_without_oauth)

    with TestClient(app, base_url=_PUBLIC_BASE_URL) as client:
        response = client.get(
            "/mcp/health", headers={"Authorization": "Bearer not-a-real-token"}
        )

    assert response.status_code == 401
    assert response.json() == _EXPECTED_BODY
