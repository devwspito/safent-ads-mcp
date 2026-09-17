"""`GET /executions`, `GET /executions/{id}`, `POST /executions/{id}/undo`
(contracts/rest-api.md §Ejecucion, deshacer y freno) de extremo a extremo:
`RequestScopedExecutionReadPort`/`ContainerSingleExecutionUndoAdapter`
reales sobre `Container`, con una propuesta aprobada de verdad (mismo
camino que `POST /proposals/{id}/approve`, `test_execution_rest.py`) para
que haya una fila de `executions` real que leer y deshacer."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from tests.contracts.execution.conftest import GuardrailLimits, seed_guardrails
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.integration.composition.conftest import AuthenticatedSession
from tests.unit.composition.factories import build_api_settings

from safent_ads.composition.api import _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.composition.execution_undo_adapter import ContainerSingleExecutionUndoAdapter
from safent_ads.execution.domain.guardrails import GuardrailScope, ScopeKind
from safent_ads.execution.infrastructure.sql_execution_read_port import (
    RequestScopedExecutionReadPort,
)
from safent_ads.execution.presentation.rest import build_execution_read_router
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import AuthenticatedCaller, get_authenticated_caller
from safent_ads.proposals.application.submit_approval import SubmitApprovalCommand
from safent_ads.proposals.domain.authorization import AuthorizationChannel
from safent_ads.proposals.domain.cause import Cause, CauseKey
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Priority, Urgency
from safent_ads.proposals.domain.proposal import Proposal, ProposedDiff, new_proposal_id
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.shared.ids import BusinessId, EntityRef

pytestmark = pytest.mark.integration


def _scope(entity_ref: EntityRef) -> GuardrailScope:
    return GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref))


@dataclass(frozen=True, slots=True)
class _ApprovedExecution:
    container: Container
    business_id: uuid.UUID
    execution_id: str


@pytest.fixture
async def approved_execution(isolated_database_url: str) -> AsyncIterator[_ApprovedExecution]:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    now = datetime.now(UTC)
    entity_ref = campaign_ref(f"c-{uuid.uuid4().hex[:10]}-exec-rest", platform_value="google")
    diff = ProposedDiff.build(
        entity_ref=entity_ref,
        parameter="daily_budget",
        before=Money.of("100"),
        after=Money.of("70"),
    )
    async with container.session_factory() as session:
        business_id = await seed_entity(session, entity_ref)
        await seed_guardrails(
            session, scope=_scope(entity_ref), limits=GuardrailLimits(), level="business"
        )
        proposal = Proposal.raise_proposal(
            proposal_id=new_proposal_id(),
            business_id=BusinessId(business_id),
            diff=diff,
            classification=Classification.ROUTINE,
            cause=Cause(text="CPL sobre objetivo en 7D", rule_id=None),
            cause_key=CauseKey(entity_ref=entity_ref, rule_id="agent", cause_type="test"),
            evidence=(),
            estimated_impact=Money.of("30"),
            priority=Priority(urgency=Urgency.RECOMMENDED),
            now=now,
            expires_at=now + timedelta(hours=24),
            expected_state_hash="a" * 64,
        )
        await SqlProposalRepository(session).save(proposal)
        use_cases = container.build_execution_use_cases(session)
        result = await use_cases.submit_approval.execute(
            SubmitApprovalCommand(
                proposal_id=proposal.proposal_id,
                diff_hash=diff.diff_hash,
                approved_by="owner-de-contrato",
                channel=AuthorizationChannel.PANEL,
            )
        )
        await session.commit()
    try:
        yield _ApprovedExecution(
            container=container, business_id=business_id, execution_id=result.execution_id
        )
    finally:
        await container.aclose()


@pytest.fixture
async def executed_execution(approved_execution: _ApprovedExecution) -> _ApprovedExecution:
    """Promueve la fila de `approved_execution` (CLAIMED, propuesta
    SCHEDULED) a EXECUTED de verdad -- lo minimo que `UndoExecution.
    _undo_executed` necesita para poder deshacer. No pasa por el broker
    real ni por la gracia servidora (eso ya lo cubre `tests/e2e/
    test_us2_defensive_autonomy.py`): las transiciones de dominio toman
    `now` como parametro explicito, asi que basta un `now` mas alla de la
    gracia -- el reloj real del contenedor (`SystemClock`) no se toca.
    2 minutos: mas alla de la gracia servidora (~20s) y muy por debajo de
    las 24h de `expires_at` (`approved_execution`)."""
    container = approved_execution.container
    now = datetime.now(UTC) + timedelta(minutes=2)
    state_hash_after = hashlib.sha256(
        f"after-{approved_execution.execution_id}".encode()
    ).hexdigest()
    async with container.session_factory() as session:
        use_cases = container.build_execution_use_cases(session)
        row = (
            await session.execute(
                text("SELECT proposal_id FROM executions WHERE id = :id"),
                {"id": approved_execution.execution_id},
            )
        ).one()
        proposal_id = ProposalId(uuid.UUID(str(row.proposal_id)))
        proposal = await use_cases.proposals.get(proposal_id)
        assert proposal is not None
        # El trigger de `proposals` (data-model.md) solo permite transiciones
        # de UN paso: `scheduled -> executed` directo lo rechaza, hay que
        # pasar por `executing` con su propio `save()`.
        proposal.begin_execution(now)
        await use_cases.proposals.save(proposal)
        proposal.record_execution(success=True, now=now)
        await use_cases.proposals.save(proposal)

        attempt = await use_cases.execution_queue.get_for_proposal(proposal_id)
        assert attempt is not None
        attempt.start_running()
        # Gracia amplia (1 hora desde un `now` ya en el futuro): el reloj
        # REAL de la peticion HTTP sigue muy por debajo, asi que "Deshacer"
        # cae en el camino RESTORED, no en el compensatorio.
        attempt.succeed("70", state_hash_after, now + timedelta(hours=1), now)
        await use_cases.execution_queue.save(attempt)
        await session.commit()
    return approved_execution


def _app(container: Container) -> FastAPI:
    application = FastAPI()
    application.state.container = container
    application.add_exception_handler(ApiError, _handle_api_error)
    application.include_router(
        build_execution_read_router(
            RequestScopedExecutionReadPort(container.session_factory),
            ContainerSingleExecutionUndoAdapter(container),
        )
    )
    return application


def _client(container: Container, cookies: dict[str, str]) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=_app(container))
    return httpx.AsyncClient(transport=transport, base_url="http://test", cookies=cookies)


async def test_list_executions_returns_the_seeded_row(
    approved_execution: _ApprovedExecution, authenticated_session: AuthenticatedSession
) -> None:
    async with _client(approved_execution.container, authenticated_session.cookies) as client:
        response = await client.get(
            "/api/v1/executions", params={"business_id": str(approved_execution.business_id)}
        )

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["execution_id"] for item in items] == [approved_execution.execution_id]
    assert items[0]["outcome"] == "CLAIMED"


async def test_get_execution_returns_the_shape(
    approved_execution: _ApprovedExecution, authenticated_session: AuthenticatedSession
) -> None:
    async with _client(approved_execution.container, authenticated_session.cookies) as client:
        response = await client.get(f"/api/v1/executions/{approved_execution.execution_id}")

    assert response.status_code == 200
    assert response.json()["execution_id"] == approved_execution.execution_id


async def test_get_execution_404_for_unknown_id(
    approved_execution: _ApprovedExecution, authenticated_session: AuthenticatedSession
) -> None:
    async with _client(approved_execution.container, authenticated_session.cookies) as client:
        response = await client.get(f"/api/v1/executions/{uuid.uuid4()}")

    assert response.status_code == 404


async def test_undo_cancels_the_scheduled_execution_then_a_second_undo_is_409(
    approved_execution: _ApprovedExecution, authenticated_session: AuthenticatedSession
) -> None:
    async with _client(approved_execution.container, authenticated_session.cookies) as client:
        first = await client.post(
            f"/api/v1/executions/{approved_execution.execution_id}/undo",
            json={"reason": "cambio de opinion"},
        )
        second = await client.post(
            f"/api/v1/executions/{approved_execution.execution_id}/undo",
            json={"reason": "otra vez"},
        )

    assert first.status_code == 200
    assert first.json()["undo_kind"] == "cancelled"
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "UNDO_WINDOW_CLOSED"


async def test_undo_within_grace_marks_the_row_and_a_second_undo_is_409_already_undone(
    executed_execution: _ApprovedExecution, authenticated_session: AuthenticatedSession
) -> None:
    """Bug corregido (esta rama): `executions.undone_at`/
    `compensating_proposal_id` no se escribian -- el panel no podia
    mostrar "deshecha" y nada impedia deshacer la misma fila dos veces."""
    async with _client(executed_execution.container, authenticated_session.cookies) as client:
        undo_response = await client.post(
            f"/api/v1/executions/{executed_execution.execution_id}/undo",
            json={"reason": "cambio de opinion"},
        )
        get_response = await client.get(
            f"/api/v1/executions/{executed_execution.execution_id}"
        )
        second_undo = await client.post(
            f"/api/v1/executions/{executed_execution.execution_id}/undo",
            json={"reason": "otra vez"},
        )

    assert undo_response.status_code == 200
    body = undo_response.json()
    assert body["undo_kind"] == "compensated"
    assert body["compensating_proposal_id"] is not None

    assert get_response.status_code == 200
    read_body = get_response.json()
    assert read_body["outcome"] == "UNDONE"
    assert read_body["undone_at"] is not None
    assert read_body["compensating_proposal_id"] == body["compensating_proposal_id"]

    assert second_undo.status_code == 409
    assert second_undo.json()["error"]["code"] == "EXECUTION_ALREADY_UNDONE"


async def test_get_execution_404_for_a_business_the_caller_cannot_see(
    approved_execution: _ApprovedExecution, authenticated_session: AuthenticatedSession
) -> None:
    app = _app(approved_execution.container)

    async def _restricted() -> AuthenticatedCaller:
        return AuthenticatedCaller(allowed_business_ids=frozenset({str(uuid.uuid4())}))

    app.dependency_overrides[get_authenticated_caller] = _restricted
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", cookies=authenticated_session.cookies
    ) as client:
        response = await client.get(f"/api/v1/executions/{approved_execution.execution_id}")

    assert response.status_code == 404
