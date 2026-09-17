"""Barrida IDOR (threat-model.md C-27, contracts/rest-api.md: "sesion sin
acceso al negocio -> 404, nunca 403") sobre las rutas NUEVAS de esta rama:
`POST /proposals/batch/approve`, `PUT /rules/{id}`, `PUT /guardrails/{id}`,
`GET /rules/autonomy-gate` y `POST /rules/autonomy-gate/confirmations`.

Dos capas se prueban por separado, con dos negocios reales sembrados:
1. `require_business_access` (panel.presentation.deps): el `business_id`
   de query param no es accesible para el caller, o no existe -- 404 antes
   de que el handler haga nada.
2. El propio handler: el `business_id` de query SI es accesible (existe,
   caller autorizado), pero el recurso referenciado (`account_ref`,
   `platform_account_id`, un `proposal_id` del lote) pertenece a OTRO
   negocio -- 404 igualmente, nunca 403 (mismo criterio que
   `tests/unit/panel/presentation/test_rest.py::test_idor_sweep`, aqui
   contra Postgres real porque estas rutas viven sobre `Container`)."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from tests.contracts.sql_fixtures import account_external_id, campaign_ref, seed_entity
from tests.unit.composition.factories import build_api_settings

from safent_ads.composition.api import _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.composition.execution_rest import build_execution_router
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import AuthenticatedCaller, get_authenticated_caller
from safent_ads.proposals.domain.cause import Cause, CauseKey
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Priority, Urgency
from safent_ads.proposals.domain.proposal import Proposal, ProposedDiff, new_proposal_id
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.shared.ids import BusinessId, EntityRef

pytestmark = pytest.mark.integration

_RAW_TOKEN = "integration-test-idor-sweep-token"  # noqa: S105 - fixture, no secreto real


class _TwoBusinesses:
    def __init__(
        self,
        *,
        business_a: uuid.UUID,
        entity_a: EntityRef,
        business_b: uuid.UUID,
        entity_b: EntityRef,
    ) -> None:
        self.business_a = business_a
        self.entity_a = entity_a
        self.account_a = f"{entity_a.platform.value}:{account_external_id(entity_a)}"
        self.business_b = business_b
        self.entity_b = entity_b
        self.account_b = f"{entity_b.platform.value}:{account_external_id(entity_b)}"


@pytest.fixture
async def two_businesses(isolated_database_url: str) -> AsyncIterator[_TwoBusinesses]:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    owner_id = uuid.uuid4()
    session_id = uuid.uuid4()
    now = datetime.now(UTC)
    entity_a = campaign_ref(f"a-{uuid.uuid4().hex[:10]}-idor")
    entity_b = campaign_ref(f"b-{uuid.uuid4().hex[:10]}-idor")

    async with AsyncSession(engine, expire_on_commit=False) as session:
        business_a = await seed_entity(session, entity_a)
        business_b = await seed_entity(session, entity_b)
        await session.execute(
            text(
                "INSERT INTO owners (id, email, password_hash) "
                "VALUES (:id, :email, :password_hash)"
            ),
            {
                "id": str(owner_id),
                "email": f"owner-{owner_id.hex[:8]}@safent.example",
                "password_hash": "argon2id$fixture$not-a-real-hash",  # noqa: S106
            },
        )
        await session.execute(
            text(
                "INSERT INTO sessions (id, owner_id, token_hash, created_at, expires_at) "
                "VALUES (:id, :owner_id, :token_hash, :created_at, :expires_at)"
            ),
            {
                "id": str(session_id),
                "owner_id": str(owner_id),
                "token_hash": hashlib.sha256(_RAW_TOKEN.encode("utf-8")).hexdigest(),
                "created_at": now,
                "expires_at": now + timedelta(hours=1),
            },
        )
        await session.commit()
    try:
        yield _TwoBusinesses(
            business_a=business_a, entity_a=entity_a, business_b=business_b, entity_b=entity_b
        )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM sessions WHERE id = :id"), {"id": str(session_id)}
            )
        await engine.dispose()


def _client_scoped_to_business_a(container: Container, business_a: uuid.UUID) -> httpx.AsyncClient:
    """Caller cuya sesion SOLO autoriza `business_a`: mismo patron que
    `tests/unit/panel/presentation/test_rest.py::client_scoped_to_business_a`,
    pero contra el router real de `Container`."""
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.include_router(build_execution_router(container))

    async def _restricted() -> AuthenticatedCaller:
        return AuthenticatedCaller(allowed_business_ids=frozenset({str(business_a)}))

    app.dependency_overrides[get_authenticated_caller] = _restricted
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://test", cookies={SESSION_COOKIE_NAME: _RAW_TOKEN}
    )


async def _seed_pending_proposal(
    container: Container, *, business_id: uuid.UUID, entity_ref: EntityRef
) -> tuple[str, str]:
    proposal_id = new_proposal_id()
    now = container.clock.now()
    async with container.session_factory() as session:
        diff = ProposedDiff.build(
            entity_ref=entity_ref,
            parameter="daily_budget",
            before=Money.of("100"),
            after=Money.of("70"),
        )
        proposal = Proposal.raise_proposal(
            proposal_id=proposal_id,
            business_id=BusinessId(business_id),
            diff=diff,
            classification=Classification.ROUTINE,
            cause=Cause(text="Contrato IDOR", rule_id=None),
            cause_key=CauseKey(entity_ref=entity_ref, rule_id="agent", cause_type="test"),
            evidence=(),
            estimated_impact=Money.of("30"),
            priority=Priority(urgency=Urgency.RECOMMENDED),
            now=now,
            expires_at=now + timedelta(hours=24),
            expected_state_hash="a" * 64,
        )
        await SqlProposalRepository(session).save(proposal)
        await session.commit()
    return str(proposal_id), diff.diff_hash


async def test_batch_approve_never_touches_a_foreign_business_proposal(
    two_businesses: _TwoBusinesses, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        proposal_id, diff_hash = await _seed_pending_proposal(
            container, business_id=two_businesses.business_b, entity_ref=two_businesses.entity_b
        )
        async with _client_scoped_to_business_a(container, two_businesses.business_a) as client:
            response = await client.post(
                "/api/v1/proposals/batch/approve",
                params={"business_id": str(two_businesses.business_a)},
                json={
                    "cause_key": "test",
                    "items": [{"proposal_id": proposal_id, "diff_hash": diff_hash}],
                },
            )
        assert response.status_code == 207, response.text
        body = response.json()
        assert body["approved_count"] == 0
        assert body["results"][0]["error_code"] == "NOT_FOUND"
    finally:
        await container.aclose()


async def test_guardrail_route_404_when_caller_lacks_business_access(
    two_businesses: _TwoBusinesses, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with _client_scoped_to_business_a(container, two_businesses.business_a) as client:
            response = await client.put(
                f"/api/v1/guardrails/{two_businesses.account_b}",
                params={"business_id": str(two_businesses.business_b)},
                json={
                    "daily_cap": "400",
                    "monthly_cap": "8000",
                    "budget_floor": "10",
                    "budget_ceiling": "250",
                    "max_step_pct": 20,
                    "max_changes_per_entity_per_day": 3,
                },
            )
        assert response.status_code == 404
    finally:
        await container.aclose()


async def test_guardrail_route_404_when_account_belongs_to_another_real_business(
    two_businesses: _TwoBusinesses, isolated_database_url: str
) -> None:
    """`business_id` de query es accesible y existe (business_a): el 404
    lo pone el handler al descubrir que `account_b` es de business_b, no
    la dependencia de acceso -- la segunda capa de defensa."""
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with _client_scoped_to_business_a(container, two_businesses.business_a) as client:
            response = await client.put(
                f"/api/v1/guardrails/{two_businesses.account_b}",
                params={"business_id": str(two_businesses.business_a)},
                json={
                    "daily_cap": "400",
                    "monthly_cap": "8000",
                    "budget_floor": "10",
                    "budget_ceiling": "250",
                    "max_step_pct": 20,
                    "max_changes_per_entity_per_day": 3,
                },
            )
        assert response.status_code == 404
    finally:
        await container.aclose()


async def test_autonomy_gate_route_404_for_a_business_the_caller_cannot_see(
    two_businesses: _TwoBusinesses, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with _client_scoped_to_business_a(container, two_businesses.business_a) as client:
            response = await client.get(
                "/api/v1/rules/autonomy-gate",
                params={"business_id": str(two_businesses.business_b)},
            )
        assert response.status_code == 404
    finally:
        await container.aclose()


async def test_autonomy_gate_confirmation_404_for_an_account_of_another_business(
    two_businesses: _TwoBusinesses, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with _client_scoped_to_business_a(container, two_businesses.business_a) as client:
            response = await client.post(
                "/api/v1/rules/autonomy-gate/confirmations",
                headers={"X-Reauth-Token": "000000"},
                json={
                    "platform_account_id": two_businesses.account_b,
                    "key": "q2_autonomous_decrease",
                    "value": "true",
                },
            )
        # `account_b` es de `business_b`, fuera del alcance del caller: 404
        # antes de siquiera pedir el TOTP (orden del handler en
        # execution_rest.py::post_autonomy_gate_confirmation).
        assert response.status_code == 404
    finally:
        await container.aclose()


async def test_put_rule_route_404_for_a_business_the_caller_cannot_see(
    two_businesses: _TwoBusinesses, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with _client_scoped_to_business_a(container, two_businesses.business_a) as client:
            response = await client.put(
                "/api/v1/rules/X93",
                params={"business_id": str(two_businesses.business_b)},
                json={"autonomy_level": "notify"},
            )
        assert response.status_code == 404
    finally:
        await container.aclose()
