"""Rutas REST de lectura (T046/T110, contracts/rest-api.md): sweep IDOR
sobre toda ruta registrada, threat-model.md C-27."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from safent_ads.panel.presentation.deps import (
    AuthenticatedCaller,
    ensure_business_access,
    get_authenticated_caller,
    require_business_access,
)
from safent_ads.panel.presentation.rest import build_panel_router
from safent_ads.panel.testing.fakes import BUSINESS_A, BUSINESS_B, FakePanelReadPort

_ENTITY_B = "google:campaign:2222222222"
_SIGNAL_B = "sig-b-1"

# Una ruta, un caso concreto contra recursos de BUSINESS_B: mas explicito y
# robusto que reflexionar sobre `app.routes` con parametros genericos, y
# cubre las 12 rutas de esta lane una por una.
_ROUTE_CASES: list[tuple[str, dict[str, str]]] = [
    ("/api/v1/portfolio", {"business_id": BUSINESS_B}),
    ("/api/v1/freshness", {"business_id": BUSINESS_B}),
    (f"/api/v1/entities/{_ENTITY_B}", {}),
    (f"/api/v1/entities/{_ENTITY_B}/children", {}),
    (f"/api/v1/entities/{_ENTITY_B}/metrics", {}),
    (f"/api/v1/entities/{_ENTITY_B}/history", {}),
    ("/api/v1/signals", {"business_id": BUSINESS_B}),
    (f"/api/v1/signals/{_SIGNAL_B}", {}),
    (f"/api/v1/signals/{_SIGNAL_B}/outcome", {}),
    ("/api/v1/anomalies", {"business_id": BUSINESS_B}),
    ("/api/v1/pacing", {"entity_ref": _ENTITY_B}),
    ("/api/v1/badges", {"business_id": BUSINESS_B}),
]


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    application.include_router(build_panel_router(FakePanelReadPort()))
    return application


@pytest.fixture
def client_scoped_to_business_a(app: FastAPI) -> Iterator[TestClient]:
    """Simula la sesion `iam` (aun no cableada): el caller solo puede ver
    `BUSINESS_A`, exactamente como hara la sesion real (placeholder
    documentado en `panel/presentation/deps.py`)."""

    async def _restricted() -> AuthenticatedCaller:
        return AuthenticatedCaller(allowed_business_ids=frozenset({BUSINESS_A}))

    app.dependency_overrides[get_authenticated_caller] = _restricted
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def unrestricted_client(app: FastAPI) -> Iterator[TestClient]:
    """Este archivo prueba el enrutado de `panel/rest.py` sobre un
    `FakePanelReadPort`, no la sesion real de `iam`: sobrescribe tanto el
    caller como `require_business_access` para saltar la comprobacion de
    existencia contra Postgres (`iam.presentation.dependencies`), que ya
    tiene su propia cobertura en `tests/unit/iam`. La restriccion por
    negocio se sigue probando de verdad via `ensure_business_access`."""

    async def _unrestricted() -> AuthenticatedCaller:
        return AuthenticatedCaller(allowed_business_ids=None)

    async def _require_business_access_without_db(business_id: str) -> str:
        ensure_business_access(business_id, await _unrestricted())
        return business_id

    app.dependency_overrides[get_authenticated_caller] = _unrestricted
    app.dependency_overrides[require_business_access] = _require_business_access_without_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_idor_sweep(client_scoped_to_business_a: TestClient) -> None:
    """threat-model.md C-27: cada una de las 12 rutas de esta lane, pedida
    con un recurso que pertenece a `BUSINESS_B` desde una sesion solo
    autorizada para `BUSINESS_A`, responde 404 (nunca 200, nunca 403 —
    contracts/rest-api.md: "para no filtrar existencia")."""
    for path, params in _ROUTE_CASES:
        response = client_scoped_to_business_a.get(path, params=params)
        assert response.status_code == 404, f"{path} devolvio {response.status_code}"


def test_authorized_business_returns_200(unrestricted_client: TestClient) -> None:
    response = unrestricted_client.get("/api/v1/portfolio", params={"business_id": BUSINESS_A})
    assert response.status_code == 200
    body = response.json()
    assert body["window"] == "7D"
    assert body["caps"]["source"] == "guardrail"
    assert body["pacing"]["days_remaining"] == 9
    assert body["spend"]["today"]["amount"] == 90
    assert body["degraded_accounts"] == []
    assert body["is_partial"] is False
    assert len(body["rows"][0]["spend_14d"]) == 14
    assert body["rows"][0]["status"] == "ACTIVE"
    assert body["rows"][0]["signal"]["kind"] == "SELL"


def test_portfolio_rejects_unknown_window(unrestricted_client: TestClient) -> None:
    response = unrestricted_client.get(
        "/api/v1/portfolio", params={"business_id": BUSINESS_A, "window": "1Y"}
    )
    assert response.status_code == 422


def test_entity_route_returns_200_for_owned_entity(unrestricted_client: TestClient) -> None:
    entity_a = "google:campaign:1111111111"
    response = unrestricted_client.get(f"/api/v1/entities/{entity_a}")
    assert response.status_code == 200
    assert response.json()["entity_ref"] == entity_a


def test_unknown_entity_ref_is_404(unrestricted_client: TestClient) -> None:
    response = unrestricted_client.get("/api/v1/entities/google:campaign:doesnotexist")
    assert response.status_code == 404


@pytest.mark.parametrize("suffix", ["children", "metrics", "history"])
def test_entity_subroutes_are_not_swallowed_by_greedy_reference(
    unrestricted_client: TestClient,
    suffix: str,
) -> None:
    response = unrestricted_client.get(f"/api/v1/entities/google:campaign:1111111111/{suffix}")
    assert response.status_code == 200


def test_children_response_matches_panel_metadata_and_honest_missing_metrics(
    unrestricted_client: TestClient,
) -> None:
    body = unrestricted_client.get("/api/v1/entities/google:campaign:1111111111/children").json()
    assert body["parent_ref"] == "google:campaign:1111111111"
    assert body["parent_name"] == "Búsqueda Marca"
    assert body["level"] == "ad"
    child = body["items"][0]
    assert child["status"] == "ACTIVE"
    assert child["spend_today"] is None
    assert child["spend_window"] is None
    assert child["conversions_by_kind"] is None
    assert child["freshness"] is None
    assert child["has_children"] is False
    fixture = Path(__file__).resolve().parents[3] / "contracts" / "entity-children.json"
    assert body == json.loads(fixture.read_text())


def test_min_strength_out_of_range_is_422(unrestricted_client: TestClient) -> None:
    response = unrestricted_client.get(
        "/api/v1/signals", params={"business_id": BUSINESS_A, "min_strength": 999}
    )
    assert response.status_code == 422


def test_response_body_has_no_stack_trace_on_404(client_scoped_to_business_a: TestClient) -> None:
    response = client_scoped_to_business_a.get(
        "/api/v1/portfolio", params={"business_id": BUSINESS_B}
    )
    assert response.status_code == 404
    body = response.json()
    assert "Traceback" not in str(body)


def test_signals_list_embeds_five_state_outcome_and_sample(
    unrestricted_client: TestClient,
) -> None:
    response = unrestricted_client.get("/api/v1/signals", params={"business_id": BUSINESS_A})
    assert response.status_code == 200
    body = response.json()
    outcome = body["items"][0]["outcome"]
    assert body["items"][0]["kind"] == "SELL"
    assert outcome["status"] in {
        "pending",
        "in_progress",
        "confirmed",
        "not_confirmed",
        "not_applicable",
    }
    assert outcome is not None
    assert body["confirmed_rate_sample"] == 3


def test_signal_outcome_route_aliases_embedded_outcome(unrestricted_client: TestClient) -> None:
    """`/signals/{id}/outcome` ya no es el antiguo booleano `matched`: es el
    mismo `outcome` de cinco estados embebido en `/signals` (alias, no ruta
    nueva) — contracts/rest-api.md §Senales."""
    signal_a = "sig-a-1"
    response = unrestricted_client.get(f"/api/v1/signals/{signal_a}/outcome")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {
        "pending",
        "in_progress",
        "confirmed",
        "not_confirmed",
        "not_applicable",
    }
    assert "matched" not in body


def test_badges_returns_200_for_owned_business(unrestricted_client: TestClient) -> None:
    response = unrestricted_client.get("/api/v1/badges", params={"business_id": BUSINESS_A})
    assert response.status_code == 200
    body = response.json()
    assert body["proposals"] == {"pending": 3, "critical": 1, "deferred": 1}
    assert body["connections"]["level"] == "ok"
    assert body["brake"]["engaged"] is False
