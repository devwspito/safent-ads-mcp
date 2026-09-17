"""`POST /api/v1/proposals/{id}/approve` de extremo a extremo: cookie de
sesion real -> `SubmitApproval` -> propuesta `SCHEDULED` + fila en
`executions` -- la misma costura que usara el panel (y, mas adelante,
Telegram por el mismo camino). Mismo patron de aislamiento que
`tests/integration/panel/test_authorization_integration.py` (commit real +
`httpx.ASGITransport`, `Container` abre su propio motor)."""

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
from tests.contracts.execution.conftest import GuardrailLimits, seed_guardrails
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.unit.composition.factories import build_api_settings

from safent_ads.composition.container import Container
from safent_ads.composition.execution_rest import build_execution_router
from safent_ads.execution.domain.guardrails import GuardrailScope, ScopeKind
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME
from safent_ads.proposals.domain.cause import Cause, CauseKey
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Priority, Urgency
from safent_ads.proposals.domain.proposal import Proposal, ProposedDiff, new_proposal_id
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.shared.ids import BusinessId, EntityRef

pytestmark = pytest.mark.integration

_RAW_TOKEN = "integration-test-execution-rest-token"  # noqa: S105 - fixture, no secreto real


class _Seeded:
    def __init__(self, business_id: uuid.UUID, proposal_id: str, diff_hash: str) -> None:
        self.business_id = business_id
        self.proposal_id = proposal_id
        self.diff_hash = diff_hash


@pytest.fixture
async def seeded_pending_proposal(isolated_database_url: str) -> AsyncIterator[_Seeded]:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    owner_id = uuid.uuid4()
    session_id = uuid.uuid4()
    now = datetime.now(UTC)
    entity_ref = campaign_ref(f"c-{uuid.uuid4().hex[:10]}-rest")

    business_id: uuid.UUID
    proposal_id = new_proposal_id()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        business_id = await seed_entity(session, entity_ref)
        await seed_guardrails(
            session,
            scope=_scope(entity_ref),
            limits=GuardrailLimits(),
            level="business",
        )
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
            cause=Cause(text="ROAS por debajo del objetivo en 7D", rule_id=None),
            cause_key=CauseKey(entity_ref=entity_ref, rule_id="agent", cause_type="test"),
            evidence=(),
            estimated_impact=Money.of("30"),
            priority=Priority(urgency=Urgency.RECOMMENDED),
            now=now,
            expires_at=now + timedelta(hours=24),
            # `tests.contracts.sql_fixtures.seed_entity` fija el
            # `platform_state_hash` de la entidad a 64 "a" -- mismo valor
            # aqui para que `executions.platform_state_hash_before` (NOT
            # NULL) tenga de donde salir.
            expected_state_hash="a" * 64,
        )
        await SqlProposalRepository(session).save(proposal)
        await session.execute(
            text(
                "INSERT INTO owners (id, email, password_hash) "
                "VALUES (:id, :email, :password_hash)"
            ),
            {
                "id": str(owner_id),
                "email": f"owner-{owner_id.hex[:8]}@safent.example",
                "password_hash": "argon2id$fixture$not-a-real-hash",
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
        yield _Seeded(business_id, str(proposal_id), diff.diff_hash)
    finally:
        # `approvals`/`proposals`/`executions` son solo-anexables (trigger
        # rechaza el DELETE, data-model.md invariante C-19): esta base es
        # `ads_isolated`, recreada entera por sesion de pytest
        # (`tests/conftest.py::isolated_database_url`), asi que dejar sus
        # filas no afecta a otros tests -- solo se limpia lo que SI se
        # puede borrar (sesion, para no dejar una cookie valida colgada).
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM sessions WHERE id = :id"), {"id": str(session_id)}
            )
        await engine.dispose()


def _scope(entity_ref: EntityRef) -> GuardrailScope:
    return GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref))


async def test_panel_read_routes_require_session_and_preserve_approval_hash(
    seeded_pending_proposal: _Seeded, isolated_database_url: str
) -> None:
    container = Container.build(build_api_settings(database_url=isolated_database_url))
    try:
        app = FastAPI()
        app.state.container = container
        app.include_router(build_execution_router(container))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            scope = {"business_id": str(seeded_pending_proposal.business_id)}
            detail_url = f"/api/v1/proposals/{seeded_pending_proposal.proposal_id}"
            assert (await client.get("/api/v1/proposals", params=scope)).status_code == 401
            assert (await client.get(detail_url, params=scope)).status_code == 401
            client.cookies.set(SESSION_COOKIE_NAME, _RAW_TOKEN)
            inbox = await client.get("/api/v1/proposals", params=scope)
            assert inbox.status_code == 200, inbox.text
            assert inbox.json()["groups"][0]["proposals"][0]["diff"]["diff_hash"] == (
                seeded_pending_proposal.diff_hash
            )
            detail = await client.get(detail_url, params=scope)
            assert detail.status_code == 200, detail.text
            assert detail.json()["diff"]["diff_hash"] == seeded_pending_proposal.diff_hash
            assert detail.json()["state"] == "pending"
            assert (await client.get(detail_url)).status_code == 422
            denied = await client.get(detail_url, params={"business_id": str(uuid.uuid4())})
            assert denied.status_code == 404
            bad_cursor = await client.get("/api/v1/proposals", params={**scope, "cursor": -1})
            assert bad_cursor.status_code == 422
    finally:
        await container.aclose()


async def test_approve_proposal_end_to_end_through_the_real_router(
    seeded_pending_proposal: _Seeded, isolated_database_url: str
) -> None:
    settings = build_api_settings(
        database_url=isolated_database_url,
        broker_socket_path="/tmp/safent-ads-wiring-b-test/execution-rest.sock",
    )
    container = Container.build(settings)
    try:
        app = FastAPI()
        app.state.container = container
        app.include_router(build_execution_router(container))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={SESSION_COOKIE_NAME: _RAW_TOKEN},
        ) as client:
            response = await client.post(
                f"/api/v1/proposals/{seeded_pending_proposal.proposal_id}/approve",
                json={"diff_hash": seeded_pending_proposal.diff_hash},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["execution_id"]
        assert body["grace_seconds"] == 20

        async with container.session_factory() as session:
            row = (
                await session.execute(
                    text("SELECT state FROM proposals WHERE id = :id"),
                    {"id": seeded_pending_proposal.proposal_id},
                )
            ).one()
            assert row.state == "scheduled"
    finally:
        await container.aclose()


async def test_batch_undo_returns_the_real_id_and_is_idempotent_end_to_end(
    seeded_pending_proposal: _Seeded, isolated_database_url: str
) -> None:
    """Bug corregido (esta rama): `POST /executions/undo` (lote, `_undo_one`
    en `composition/execution_rest.py`) siempre devolvia
    `compensating_proposal_id` implicito a `None` -- `UndoExecution`
    (execution.application) nunca marcaba la fila original ni exponia el id
    de la propuesta compensatoria."""
    settings = build_api_settings(
        database_url=isolated_database_url,
        broker_socket_path="/tmp/safent-ads-wiring-b-test/execution-rest-batch.sock",
    )
    container = Container.build(settings)
    try:
        app = FastAPI()
        app.state.container = container
        app.include_router(build_execution_router(container))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={SESSION_COOKIE_NAME: _RAW_TOKEN},
        ) as client:
            approve_response = await client.post(
                f"/api/v1/proposals/{seeded_pending_proposal.proposal_id}/approve",
                json={"diff_hash": seeded_pending_proposal.diff_hash},
            )
            assert approve_response.status_code == 200, approve_response.text
            execution_id = approve_response.json()["execution_id"]

            # Promueve CLAIMED/SCHEDULED a EXECUTED sin esperar la gracia
            # real ni el broker (eso ya lo cubre `tests/e2e/
            # test_us2_defensive_autonomy.py`): las transiciones de dominio
            # toman `now` explicito, asi que un `now` un poco por delante
            # del reloj real del contenedor (`SystemClock`) basta -- 2
            # minutos: mas alla de la gracia servidora (~20s) y muy por
            # debajo de las 24h de `expires_at` (`seeded_pending_proposal`).
            now = datetime.now(UTC) + timedelta(minutes=2)
            state_hash_after = hashlib.sha256(f"after-{execution_id}".encode()).hexdigest()
            async with container.session_factory() as session:
                use_cases = container.build_execution_use_cases(session)
                proposal_id = ProposalId.parse(seeded_pending_proposal.proposal_id)
                proposal = await use_cases.proposals.get(proposal_id)
                assert proposal is not None
                # El trigger de `proposals` (data-model.md) solo permite
                # transiciones de UN paso: `scheduled -> executed` directo lo
                # rechaza, hay que pasar por `executing` con su propio `save()`.
                proposal.begin_execution(now)
                await use_cases.proposals.save(proposal)
                proposal.record_execution(success=True, now=now)
                await use_cases.proposals.save(proposal)

                attempt = await use_cases.execution_queue.get_for_proposal(proposal_id)
                assert attempt is not None
                attempt.start_running()
                attempt.succeed("70", state_hash_after, now + timedelta(hours=1), now)
                await use_cases.execution_queue.save(attempt)
                await session.commit()

            first = await client.post(
                "/api/v1/executions/undo",
                json={"execution_ids": [execution_id], "reason": "cambio de opinion"},
            )
            second = await client.post(
                "/api/v1/executions/undo",
                json={"execution_ids": [execution_id], "reason": "otra vez"},
            )

        assert first.status_code == 200, first.text
        first_result = first.json()["results"][0]
        assert first_result["ok"] is True
        assert first_result["compensating_proposal_id"] is not None

        assert second.status_code == 200, second.text
        second_result = second.json()["results"][0]
        assert second_result["ok"] is False
        assert second_result["error_code"] == "EXECUTION_ALREADY_UNDONE"

        async with container.session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT undone_at, compensating_proposal_id FROM executions "
                        "WHERE id = :id"
                    ),
                    {"id": execution_id},
                )
            ).one()
            assert row.undone_at is not None
            assert str(row.compensating_proposal_id) == first_result["compensating_proposal_id"]
    finally:
        await container.aclose()


async def test_proposals_inbox_etag_turns_a_repeat_poll_into_a_304(
    seeded_pending_proposal: _Seeded, isolated_database_url: str
) -> None:
    """Perf (16-sep, item 5): the panel polls `GET /api/v1/proposals` every
    45 s (`useProposals`, `refetchInterval`) -- a matching `If-None-Match`
    must cost a `304` with an empty body, not the full inbox again."""
    container = Container.build(build_api_settings(database_url=isolated_database_url))
    try:
        app = FastAPI()
        app.state.container = container
        app.include_router(build_execution_router(container))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={SESSION_COOKIE_NAME: _RAW_TOKEN},
        ) as client:
            scope = {"business_id": str(seeded_pending_proposal.business_id)}

            first = await client.get("/api/v1/proposals", params=scope)
            assert first.status_code == 200, first.text
            assert first.headers["cache-control"] == "private, max-age=0, must-revalidate"
            etag = first.headers["etag"]

            second = await client.get(
                "/api/v1/proposals", params=scope, headers={"if-none-match": etag}
            )

            assert second.status_code == 304
            assert second.content == b""
            assert second.headers["etag"] == etag
    finally:
        await container.aclose()


async def test_proposals_inbox_etag_changes_once_the_proposal_is_approved(
    seeded_pending_proposal: _Seeded, isolated_database_url: str
) -> None:
    """A stale `If-None-Match` from before the approval must not shadow the
    new state: the inbox body (and therefore its `ETag`) changed, so this
    must be a full `200`, never a `304`."""
    container = Container.build(build_api_settings(database_url=isolated_database_url))
    try:
        app = FastAPI()
        app.state.container = container
        app.include_router(build_execution_router(container))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={SESSION_COOKIE_NAME: _RAW_TOKEN},
        ) as client:
            scope = {"business_id": str(seeded_pending_proposal.business_id)}
            stale_etag = (await client.get("/api/v1/proposals", params=scope)).headers["etag"]

            approved = await client.post(
                f"/api/v1/proposals/{seeded_pending_proposal.proposal_id}/approve",
                json={"diff_hash": seeded_pending_proposal.diff_hash},
            )
            assert approved.status_code == 200, approved.text

            after = await client.get(
                "/api/v1/proposals", params=scope, headers={"if-none-match": stale_etag}
            )

            assert after.status_code == 200
            assert after.headers["etag"] != stale_etag
    finally:
        await container.aclose()
