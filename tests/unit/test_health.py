"""GET /api/v1/health responde 200 sin autenticacion (contracts/rest-api.md:
"GET /health -> 200 liveness, sin autenticacion, sin detalle")."""

from __future__ import annotations

from fastapi.testclient import TestClient

from safent_ads.composition.app import create_app
from safent_ads.composition.settings import ApiSettings


def test_health_returns_200_ok(api_settings: ApiSettings) -> None:
    app = create_app(api_settings)

    with TestClient(app) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_response_has_no_extra_detail(api_settings: ApiSettings) -> None:
    app = create_app(api_settings)

    with TestClient(app) as client:
        response = client.get("/api/v1/health")

    assert set(response.json().keys()) == {"status"}
