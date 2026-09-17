"""`POST /api/v1/entities/{entity_ref}/pause|resume|delete` de extremo a
extremo (design.md §0.5-0.7): cookie de sesion real
-> `PauseEntity`/`ResumeEntity`/`DeleteEntity` -> MISMO `ExecutionChokepoint`
que una propuesta aprobada -> `executions` auditado. Mismo patron de
aislamiento que `tests/integration/composition/test_execution_rest.py`
(commit real + `httpx.ASGITransport`), con un `AdsPlatformPort` de
contornos propio (sin socket) en vez del `BrokerSocketClient` real, para
que el camino llegue a `EXECUTED` de verdad dentro de este banco."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from tests.contracts.execution.conftest import GuardrailLimits, seed_guardrails
from tests.contracts.sql_fixtures import account_external_id, campaign_ref, seed_entity
from tests.unit.composition.factories import build_api_settings

from safent_ads.accounts.application.ports import (
    AccountRef,
    AdEntitySnapshot,
    AssetUploadRequest,
    EntityStateSnapshot,
    MetricFactSnapshot,
    MetricsRequest,
    PlatformAssetHandle,
    SignedAuthorization,
    WriteIntent,
    WriteOutcome,
)
from safent_ads.accounts.domain.ad_entity import AdEntityStatus
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.accounts.domain.refs import IdempotencyKey
from safent_ads.composition.api import _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.composition.execution_rest import build_execution_router
from safent_ads.execution.domain.guardrails import GuardrailScope, ScopeKind
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import AuthenticatedCaller, get_authenticated_caller
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import EntityRef

pytestmark = pytest.mark.integration

_RAW_TOKEN = "integration-test-entity-lifecycle-token"  # noqa: S105 - fixture, no secreto real
_NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
_CANONICAL_STATE = {"status": "ACTIVE", "daily_budget_minor": 10_000}
_STATE_HASH = PlatformStateHash.compute(_CANONICAL_STATE).value


class _FakePlatformPort:
    """`AdsPlatformPort` de contornos (sin socket, sin SDK): siempre
    confirma la misma foto remota, y toda escritura tiene exito -- lo que
    este banco necesita es probar el camino REST -> caso de uso ->
    chokepoint, no el mapeo fino de cada plataforma (eso ya lo cubren
    `tests/unit/broker/platforms/*`)."""

    def __init__(self) -> None:
        self.write_calls: list[WriteIntent] = []

    async def fetch_account_inventory(
        self,
        account_ref: AccountRef,  # noqa: ARG002 - forma exacta del puerto
    ) -> Sequence[AdEntitySnapshot]:
        return []

    async def fetch_metrics(
        self,
        request: MetricsRequest,  # noqa: ARG002
    ) -> Sequence[MetricFactSnapshot]:
        return []

    async def read_entity_state(self, entity_ref: EntityRef) -> EntityStateSnapshot:
        return EntityStateSnapshot(
            entity_ref=entity_ref,
            status=AdEntityStatus.ACTIVE,
            is_controllable=True,
            canonical_state=_CANONICAL_STATE,
            fetched_at=_NOW,
        )

    async def upload_asset(
        self,
        request: AssetUploadRequest,  # noqa: ARG002
    ) -> PlatformAssetHandle:
        raise NotImplementedError

    async def execute_write(
        self,
        intent: WriteIntent,
        authorization: SignedAuthorization,
        idempotency_key: IdempotencyKey,
    ) -> WriteOutcome:
        del authorization, idempotency_key
        self.write_calls.append(intent)
        return WriteOutcome(
            outcome="SUCCEEDED",
            applied_value=intent.valor_propuesto,
            state_hash_after=_STATE_HASH,
            error_code=None,
            platform_request_id="req-1",
        )

    async def read_write_receipt(
        self,
        intent: WriteIntent,  # noqa: ARG002
        authorization: SignedAuthorization,  # noqa: ARG002
        idempotency_key: IdempotencyKey,  # noqa: ARG002
    ) -> WriteOutcome | None:
        return None

    async def run_gaql(self, account_ref: AccountRef, query: str, *, max_rows: int):  # noqa: ARG002
        raise NotImplementedError


class _Seeded:
    def __init__(self, business_id: uuid.UUID, entity_ref: EntityRef) -> None:
        self.business_id = business_id
        self.entity_ref = entity_ref


@pytest.fixture
async def seeded_active_campaign(isolated_database_url: str) -> AsyncIterator[_Seeded]:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    owner_id = uuid.uuid4()
    session_id = uuid.uuid4()
    now = datetime.now(UTC)
    entity_ref = campaign_ref(f"c-{uuid.uuid4().hex[:10]}-lifecycle")

    async with AsyncSession(engine, expire_on_commit=False) as session:
        business_id = await seed_entity(session, entity_ref)
        # `max_changes_per_entity_day` por encima del default (2): este
        # banco encadena pausar + reanudar + borrar sobre la MISMA entidad
        # el mismo dia, tres apuntes reales en `spend_ledger` aunque su
        # delta sea 0 (`money_pair_from_diff` proyecta un diff de status a
        # `Money.zero()`, pero el conteo de cambios se aplica siempre).
        await seed_guardrails(
            session,
            scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref)),
            limits=GuardrailLimits(max_changes_per_entity_day=10),
            level="business",
        )
        # `seed_entity` fija `platform_state_hash` a 64 "a": este banco
        # necesita que coincida con lo que `_FakePlatformPort.read_entity_state`
        # produce de verdad, o toda escritura saldria `SKIPPED_DRIFT`.
        await session.execute(
            text("UPDATE ad_entities SET platform_state_hash = :hash WHERE entity_ref = :ref"),
            {"hash": _STATE_HASH, "ref": str(entity_ref)},
        )
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
        yield _Seeded(business_id, entity_ref)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM sessions WHERE id = :id"), {"id": str(session_id)}
            )
        await engine.dispose()


async def _set_entity_status(database_url: str, entity_ref: EntityRef, status: str) -> None:
    """Simula lo que un ciclo de sincronizacion aplicaria despues de una
    escritura real: el chokepoint nunca actualiza `ad_entities.status` el
    mismo (esa es tarea de `SyncAccountInventory`, otra rama)."""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE ad_entities SET status = :status WHERE entity_ref = :ref"),
                {"status": status, "ref": str(entity_ref)},
            )
    finally:
        await engine.dispose()


def _build_container(database_url: str) -> Container:
    settings = build_api_settings(
        database_url=database_url,
        broker_socket_path="/tmp/safent-ads-wiring-b-test/entity-lifecycle.sock",
    )
    container = Container.build(settings)
    container.clock = FixedClock(_NOW)
    container.ads_platform_port = _FakePlatformPort()
    return container


def _app(container: Container) -> FastAPI:
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.include_router(build_execution_router(container))
    return app


def _client(container: Container) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(container)),
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: _RAW_TOKEN},
    )


def _restricted_client(container: Container, allowed_business_id: uuid.UUID) -> httpx.AsyncClient:
    app = _app(container)

    async def _restricted() -> AuthenticatedCaller:
        return AuthenticatedCaller(allowed_business_ids=frozenset({str(allowed_business_id)}))

    app.dependency_overrides[get_authenticated_caller] = _restricted
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: _RAW_TOKEN},
    )


async def test_pause_resume_and_delete_end_to_end_through_the_real_router(
    seeded_active_campaign: _Seeded, isolated_database_url: str
) -> None:
    ref = seeded_active_campaign.entity_ref
    container = _build_container(isolated_database_url)
    try:
        async with _client(container) as client:
            pause = await client.post(f"/api/v1/entities/{ref}/pause")
            assert pause.status_code == 200, pause.text
            pause_body = pause.json()
            assert pause_body["execution_id"]
            assert pause_body["undo_deadline"] is not None

            await _set_entity_status(isolated_database_url, ref, "PAUSED")

            resume = await client.post(f"/api/v1/entities/{ref}/resume")
            assert resume.status_code == 200, resume.text
            resume_body = resume.json()
            assert resume_body["execution_id"]
            assert resume_body["undo_deadline"] is not None

            await _set_entity_status(isolated_database_url, ref, "ACTIVE")

            delete = await client.post(f"/api/v1/entities/{ref}/delete")
            assert delete.status_code == 200, delete.text
            delete_body = delete.json()
            assert delete_body == {"execution_id": delete_body["execution_id"]}

            undo = await client.post(
                "/api/v1/executions/undo",
                json={"execution_ids": [delete_body["execution_id"]], "reason": "cambio de idea"},
            )
        assert undo.status_code == 200, undo.text
        undo_result = undo.json()["results"][0]
        assert undo_result["ok"] is False

        async with container.session_factory() as session:
            row = (
                await session.execute(
                    text("SELECT state FROM proposals WHERE cause_key = :cause_key"),
                    {"cause_key": f"owner_delete:owner:{ref}"},
                )
            ).one()
            assert row.state == "executed"
    finally:
        await container.aclose()


async def test_pause_unknown_entity_returns_404(
    seeded_active_campaign: _Seeded, isolated_database_url: str
) -> None:
    del seeded_active_campaign
    container = _build_container(isolated_database_url)
    try:
        async with _client(container) as client:
            response = await client.post(
                "/api/v1/entities/meta:campaign:does-not-exist/pause"
            )
        assert response.status_code == 404, response.text
    finally:
        await container.aclose()


async def test_pause_denies_when_not_controllable(
    seeded_active_campaign: _Seeded, isolated_database_url: str
) -> None:
    ref = seeded_active_campaign.entity_ref
    container = _build_container(isolated_database_url)
    try:
        async with container.session_factory() as session:
            await session.execute(
                text("UPDATE ad_entities SET is_controllable = false WHERE entity_ref = :ref"),
                {"ref": str(ref)},
            )
            await session.commit()
        async with _client(container) as client:
            response = await client.post(f"/api/v1/entities/{ref}/pause")
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "NOT_CONTROLLABLE"
    finally:
        await container.aclose()


async def test_pause_denies_when_the_brake_is_engaged(
    seeded_active_campaign: _Seeded, isolated_database_url: str
) -> None:
    ref = seeded_active_campaign.entity_ref
    container = _build_container(isolated_database_url)
    try:
        async with _client(container) as client:
            # `brake_scope_from` (execution.domain.guardrails) siempre
            # comprueba el freno a nivel de CUENTA de plataforma, nunca de
            # negocio: un freno `scope_kind=business` vive en una fila
            # distinta (`emergency_brakes.scope_kind='business'`) que la
            # que lee el chokepoint para esta entidad -- se activa aqui en
            # el ambito exacto que el caso de uso resuelve.
            account_ref = f"{ref.platform.value}:{account_external_id(ref)}"
            engage = await client.post(
                "/api/v1/kill-switch",
                params={"business_id": str(seeded_active_campaign.business_id)},
                json={
                    "scope_kind": "platform_account",
                    "scope_id": account_ref,
                    "engaged": True,
                    "mode": "all",
                    "reason": "prueba de integracion",
                },
            )
            assert engage.status_code == 200, engage.text

            response = await client.post(f"/api/v1/entities/{ref}/pause")
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "BRAKE_ENGAGED"
    finally:
        await container.aclose()


async def test_pause_is_blocked_for_a_caller_scoped_to_another_business(
    seeded_active_campaign: _Seeded, isolated_database_url: str
) -> None:
    """threat-model.md C-27: un caller sin acceso al negocio dueño de la
    entidad recibe 404, nunca 403 -- ni pistas de que la entidad exista."""
    ref = seeded_active_campaign.entity_ref
    container = _build_container(isolated_database_url)
    try:
        async with _restricted_client(container, uuid.uuid4()) as client:
            response = await client.post(f"/api/v1/entities/{ref}/pause")
        assert response.status_code == 404, response.text
    finally:
        await container.aclose()
