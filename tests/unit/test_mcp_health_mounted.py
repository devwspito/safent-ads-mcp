"""`create_app` monta `GET /mcp/health` ANTES de la ruta de `/mcp`
(composition/app.py): esta ruta exacta debe ganar sobre la del transporte
streamable-http, y debe exigir el mismo `ADS_MCP_TOKEN` que `/mcp` -- nunca
un segundo secreto (composition/settings.py)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from safent_ads.composition.app import create_app
from safent_ads.composition.settings import ApiSettings


def _without_oauth(api_settings: ApiSettings) -> ApiSettings:
    """`api_settings` (tests/conftest.py) no apunta a un Postgres real: con
    `mcp_oauth_enabled=True` (el valor por defecto de produccion),
    `CompositeTokenVerifier` intentaria una consulta real antes de caer al
    bearer estatico que estas pruebas ejercitan (spec 002 mcp_oauth).

    M6 de la revision de seguridad (16-sep): `mcp_static_token_enabled` es
    `False` por defecto -- con OAuth apagado, estas pruebas SOLO pueden
    ejercitar el camino estatico, asi que lo encienden explicitamente."""
    return api_settings.model_copy(
        update={"mcp_oauth_enabled": False, "mcp_static_token_enabled": True}
    )


def test_mcp_health_route_wins_over_the_mcp_transport(api_settings: ApiSettings) -> None:
    app = create_app(_without_oauth(api_settings))

    token = api_settings.mcp_token.get_secret_value()
    with TestClient(app) as client:
        response = client.get("/mcp/health", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code != 404
    assert "contract_version" in response.json()


def test_mcp_health_rejects_missing_bearer(api_settings: ApiSettings) -> None:
    app = create_app(_without_oauth(api_settings))

    with TestClient(app) as client:
        response = client.get("/mcp/health")

    assert response.status_code == 401


def test_mcp_health_rejects_wrong_bearer(api_settings: ApiSettings) -> None:
    app = create_app(_without_oauth(api_settings))

    with TestClient(app) as client:
        response = client.get("/mcp/health", headers={"Authorization": "Bearer not-the-token"})

    assert response.status_code == 401
