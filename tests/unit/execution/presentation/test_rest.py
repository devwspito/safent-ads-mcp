"""`GET /executions`, `GET /executions/{id}`, `POST /executions/{id}/undo`
(contracts/rest-api.md §Ejecucion, deshacer y freno) sobre
`FakeExecutionReadPort`/`FakeSingleExecutionUndoPort` -- mismo patron de
`dependency_overrides` que `tests/unit/panel/presentation/test_rest.py`.
`SqlExecutionReadPort` real (joins, mapeo `outcome`) se prueba en
`tests/integration/execution/test_sql_execution_read_port.py`."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from safent_ads.composition.api import _handle_api_error
from safent_ads.execution.application.execution_read_port import ExecutionView
from safent_ads.execution.application.single_execution_undo_port import SingleUndoResult
from safent_ads.execution.presentation.rest import build_execution_read_router
from safent_ads.execution.testing.fakes import FakeExecutionReadPort, FakeSingleExecutionUndoPort
from safent_ads.iam.presentation.dependencies import AuthenticatedOwner, current_owner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import (
    AuthenticatedCaller,
    ensure_business_access,
    get_authenticated_caller,
    require_business_access,
)
from safent_ads.shared.read_models.dto import Money

_BUSINESS_A = "11111111-1111-1111-1111-111111111111"
_BUSINESS_B = "22222222-2222-2222-2222-222222222222"
_EXECUTION_A = "exec-a"
_EXECUTION_B = "exec-b"
_STARTED_AT = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _view(*, execution_id: str, business_id: str, outcome: str = "SUCCEEDED") -> ExecutionView:
    return ExecutionView(
        execution_id=execution_id,
        proposal_id="prop-1",
        business_id=business_id,
        entity_name="Campaña Otoño",
        outcome=outcome,
        applied_value=70.0,
        previous_value=100.0,
        estimated_impact=Money(amount=Decimal("30.00"), currency="EUR"),
        undo_deadline=None,
        started_at=_STARTED_AT,
        finished_at=_STARTED_AT,
    )


@pytest.fixture
def reads() -> FakeExecutionReadPort:
    return FakeExecutionReadPort(
        [
            _view(execution_id=_EXECUTION_A, business_id=_BUSINESS_A),
            _view(execution_id=_EXECUTION_B, business_id=_BUSINESS_B),
        ]
    )


@pytest.fixture
def undo() -> FakeSingleExecutionUndoPort:
    return FakeSingleExecutionUndoPort(
        {_EXECUTION_A: SingleUndoResult(ok=True, undo_kind="cancelled")}
    )


@pytest.fixture
def app(reads: FakeExecutionReadPort, undo: FakeSingleExecutionUndoPort) -> FastAPI:
    application = FastAPI()
    application.add_exception_handler(ApiError, _handle_api_error)
    application.include_router(build_execution_read_router(reads, undo))
    return application


def _fake_owner() -> AuthenticatedOwner:
    return AuthenticatedOwner(owner_id=uuid.uuid4(), email="owner@safent.example")


@pytest.fixture
def unrestricted_client(app: FastAPI) -> Iterator[TestClient]:
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


def test_list_executions_returns_only_the_requested_business(
    unrestricted_client: TestClient,
) -> None:
    response = unrestricted_client.get(
        "/api/v1/executions", params={"business_id": _BUSINESS_A}
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["execution_id"] for item in items] == [_EXECUTION_A]
    assert items[0]["estimated_impact"] == {"amount": 30.0, "currency": "EUR"}
    assert items[0]["started_at"] == _STARTED_AT.isoformat()


def test_get_execution_returns_the_shape(unrestricted_client: TestClient) -> None:
    response = unrestricted_client.get(f"/api/v1/executions/{_EXECUTION_A}")

    assert response.status_code == 200
    assert response.json()["execution_id"] == _EXECUTION_A


def test_get_execution_exposes_undone_marking() -> None:
    """Sin `undone_at`/`compensating_proposal_id` en la respuesta, el panel
    no puede pintar "deshecha" (zod `executions.ts`)."""
    undone_view = replace(
        _view(execution_id="exec-undone", business_id=_BUSINESS_A),
        undone_at=_STARTED_AT,
        compensating_proposal_id="prop-2",
    )
    application = FastAPI()
    application.add_exception_handler(ApiError, _handle_api_error)
    application.include_router(
        build_execution_read_router(
            FakeExecutionReadPort([undone_view]), FakeSingleExecutionUndoPort({})
        )
    )

    async def _unrestricted() -> AuthenticatedCaller:
        return AuthenticatedCaller(allowed_business_ids=None)

    async def _require_business_access_without_db(business_id: str) -> str:
        ensure_business_access(business_id, await _unrestricted())
        return business_id

    application.dependency_overrides[get_authenticated_caller] = _unrestricted
    application.dependency_overrides[require_business_access] = _require_business_access_without_db
    application.dependency_overrides[current_owner] = _fake_owner
    with TestClient(application) as client:
        response = client.get("/api/v1/executions/exec-undone")

    assert response.status_code == 200
    body = response.json()
    assert body["undone_at"] == _STARTED_AT.isoformat()
    assert body["compensating_proposal_id"] == "prop-2"


def test_get_execution_404_for_unknown_id(unrestricted_client: TestClient) -> None:
    response = unrestricted_client.get("/api/v1/executions/does-not-exist")

    assert response.status_code == 404


def test_undo_execution_returns_the_outcome(unrestricted_client: TestClient) -> None:
    response = unrestricted_client.post(
        f"/api/v1/executions/{_EXECUTION_A}/undo", json={"reason": "cambio de opinion"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "undo_kind": "cancelled",
        "execution_id": _EXECUTION_A,
        "compensating_proposal_id": None,
    }


def test_undo_execution_returns_the_real_compensating_proposal_id(
    unrestricted_client: TestClient, undo: FakeSingleExecutionUndoPort
) -> None:
    """Bug corregido: antes de esta rama, `SingleExecutionUndoPort` siempre
    devolvia `None` aqui aunque `UndoExecution` hubiera creado una
    propuesta compensatoria de verdad."""
    undo.results[_EXECUTION_A] = SingleUndoResult(
        ok=True, undo_kind="compensated", compensating_proposal_id="prop-restore-1"
    )

    response = unrestricted_client.post(
        f"/api/v1/executions/{_EXECUTION_A}/undo", json={"reason": "cambio de opinion"}
    )

    assert response.status_code == 200
    assert response.json()["compensating_proposal_id"] == "prop-restore-1"


def test_undo_execution_requires_a_reason(unrestricted_client: TestClient) -> None:
    response = unrestricted_client.post(f"/api/v1/executions/{_EXECUTION_A}/undo", json={})

    assert response.status_code == 422


def test_undo_execution_409_when_the_port_denies_it(
    unrestricted_client: TestClient, undo: FakeSingleExecutionUndoPort
) -> None:
    undo.results[_EXECUTION_A] = SingleUndoResult(ok=False, error_code="UNDO_WINDOW_CLOSED")

    response = unrestricted_client.post(
        f"/api/v1/executions/{_EXECUTION_A}/undo", json={"reason": "tarde"}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "UNDO_WINDOW_CLOSED"


def test_undo_execution_409_typed_conflict_when_already_undone(
    unrestricted_client: TestClient, undo: FakeSingleExecutionUndoPort
) -> None:
    """Idempotencia (FR-15): deshacer una ejecucion ya deshecha es un
    conflicto tipado propio, distinto del generico `UNDO_WINDOW_CLOSED`."""
    undo.results[_EXECUTION_A] = SingleUndoResult(ok=False, error_code="EXECUTION_ALREADY_UNDONE")

    response = unrestricted_client.post(
        f"/api/v1/executions/{_EXECUTION_A}/undo", json={"reason": "otra vez"}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EXECUTION_ALREADY_UNDONE"


def test_get_execution_404_for_a_business_the_caller_cannot_see(
    client_scoped_to_business_a: TestClient,
) -> None:
    response = client_scoped_to_business_a.get(f"/api/v1/executions/{_EXECUTION_B}")

    assert response.status_code == 404


def test_undo_execution_404_for_a_business_the_caller_cannot_see(
    client_scoped_to_business_a: TestClient,
) -> None:
    response = client_scoped_to_business_a.post(
        f"/api/v1/executions/{_EXECUTION_B}/undo", json={"reason": "no deberia poder"}
    )

    assert response.status_code == 404
