"""`GET/PUT /settings` (contracts/rest-api.md §Ajustes) sobre
`FakeSettingsRepository` -- sin Postgres, mismo patron de
`dependency_overrides` que `tests/unit/panel/presentation/test_rest.py`
para simular la sesion real de `iam`/el alcance del `caller`."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from safent_ads.composition.api import _handle_api_error
from safent_ads.iam.presentation.dependencies import AuthenticatedOwner, current_owner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import (
    AuthenticatedCaller,
    ensure_business_access,
    get_authenticated_caller,
    require_business_access,
)
from safent_ads.settings.application.get_settings import GetSettings
from safent_ads.settings.application.update_settings import UpdateSettings
from safent_ads.settings.domain.value_objects import ActiveHours, DigestHour
from safent_ads.settings.presentation.rest import build_settings_router
from safent_ads.settings.testing.fakes import FakeSettingsRepository

_BUSINESS_A = "11111111-1111-1111-1111-111111111111"
_BUSINESS_B = "22222222-2222-2222-2222-222222222222"
_OWNER_ID = uuid.uuid4()
_DEFAULT_ACTIVE_HOURS = ActiveHours.parse(start="08:00", end="21:00")
_DEFAULT_DIGEST_HOUR = DigestHour.parse("09:00")


@pytest.fixture
def repository() -> FakeSettingsRepository:
    return FakeSettingsRepository(
        {_BUSINESS_A: ("Europe/Madrid", "EUR"), _BUSINESS_B: ("Europe/Madrid", "EUR")},
        default_active_hours=_DEFAULT_ACTIVE_HOURS,
        default_digest_hour=_DEFAULT_DIGEST_HOUR,
    )


@pytest.fixture
def app(repository: FakeSettingsRepository) -> FastAPI:
    application = FastAPI()
    application.add_exception_handler(ApiError, _handle_api_error)
    application.include_router(
        build_settings_router(GetSettings(repository), UpdateSettings(repository))
    )
    return application


def _fake_owner() -> AuthenticatedOwner:
    return AuthenticatedOwner(owner_id=_OWNER_ID, email="owner@safent.example")


@pytest.fixture
def unrestricted_client(app: FastAPI) -> Iterator[TestClient]:
    """Mismo criterio que `tests/unit/panel/presentation/test_rest.py`: la
    sesion real de `iam` no esta cableada aqui, se sustituye por un
    propietario fijo y se salta la comprobacion de existencia contra
    Postgres -- `ensure_business_access` (la restriccion real por negocio)
    se sigue ejecutando de verdad."""

    async def _unrestricted() -> AuthenticatedCaller:
        return AuthenticatedCaller(allowed_business_ids=None)

    async def _require_business_access_without_db(business_id: str) -> str:
        ensure_business_access(business_id, await _unrestricted())
        return business_id

    application = app
    application.dependency_overrides[get_authenticated_caller] = _unrestricted
    application.dependency_overrides[require_business_access] = _require_business_access_without_db
    application.dependency_overrides[current_owner] = _fake_owner
    with TestClient(application) as test_client:
        yield test_client
    application.dependency_overrides.clear()


@pytest.fixture
def client_scoped_to_business_a(app: FastAPI) -> Iterator[TestClient]:
    async def _restricted() -> AuthenticatedCaller:
        return AuthenticatedCaller(allowed_business_ids=frozenset({_BUSINESS_A}))

    application = app
    application.dependency_overrides[get_authenticated_caller] = _restricted
    application.dependency_overrides[current_owner] = _fake_owner
    with TestClient(application) as test_client:
        yield test_client
    application.dependency_overrides.clear()


def test_get_settings_returns_the_default_shape(unrestricted_client: TestClient) -> None:
    response = unrestricted_client.get("/api/v1/settings", params={"business_id": _BUSINESS_A})

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "business_id": _BUSINESS_A,
        "timezone": "Europe/Madrid",
        "currency": "EUR",
        "active_hours": {"start": "08:00", "end": "21:00"},
        "digest_hour": "09:00",
        "theme": "system",
    }


def test_put_settings_persists_and_echoes_the_new_values(unrestricted_client: TestClient) -> None:
    response = unrestricted_client.put(
        "/api/v1/settings",
        params={"business_id": _BUSINESS_A},
        json={
            "active_hours": {"start": "07:00", "end": "22:00"},
            "digest_hour": "07:00",
            "theme": "dark",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["active_hours"] == {"start": "07:00", "end": "22:00"}
    assert body["digest_hour"] == "07:00"
    assert body["theme"] == "dark"

    follow_up = unrestricted_client.get("/api/v1/settings", params={"business_id": _BUSINESS_A})
    assert follow_up.json()["theme"] == "dark"


_INVERTED_ACTIVE_HOURS = {"start": "21:00", "end": "08:00"}
_VALID_ACTIVE_HOURS = {"start": "08:00", "end": "21:00"}


@pytest.mark.parametrize(
    "body",
    [
        {"active_hours": _INVERTED_ACTIVE_HOURS, "digest_hour": "09:00", "theme": "dark"},
        {"active_hours": _VALID_ACTIVE_HOURS, "digest_hour": "09:30", "theme": "dark"},
        {"active_hours": _VALID_ACTIVE_HOURS, "digest_hour": "09:00", "theme": "neon"},
        {"active_hours": _VALID_ACTIVE_HOURS, "digest_hour": "09:00"},
        {"digest_hour": "09:00", "theme": "dark"},
    ],
)
def test_put_settings_rejects_invalid_bodies(
    unrestricted_client: TestClient, body: dict[str, object]
) -> None:
    response = unrestricted_client.put(
        "/api/v1/settings", params={"business_id": _BUSINESS_A}, json=body
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_get_settings_404_for_a_business_the_caller_cannot_see(
    client_scoped_to_business_a: TestClient,
) -> None:
    response = client_scoped_to_business_a.get(
        "/api/v1/settings", params={"business_id": _BUSINESS_B}
    )

    assert response.status_code == 404


def test_put_settings_404_for_a_business_the_caller_cannot_see(
    client_scoped_to_business_a: TestClient,
) -> None:
    response = client_scoped_to_business_a.put(
        "/api/v1/settings",
        params={"business_id": _BUSINESS_B},
        json={
            "active_hours": {"start": "08:00", "end": "21:00"},
            "digest_hour": "09:00",
            "theme": "dark",
        },
    )

    assert response.status_code == 404
