"""`/api/v1/crm/*` a traves del router real sobre Postgres
(`crm.presentation.bridge_rest.build_crm_bridge_router`, contracts/
crm-link.md §2, spec 027 T016): autenticacion por `X-Bridge-Token`,
`IDENTITY_NOT_HASHED`, `REFUND_MUST_BE_NEGATIVE`, idempotencia de lote,
ningun identificador crudo en `details` de error -- mismo patron que
`test_conversions_rest.py`.

Nota de ubicacion (tech-lead, coordinacion de carriles): tasks.md T016
nombra `tests/contracts/test_crm_bridge_api.py`, pero `tests/contracts/`
en este repo aloja SOLO contratos de repositorio (memoria vs SQL,
`tests/contracts/<contexto>/`) -- las pruebas de un router REST real ya
viven en `tests/integration/composition/test_*_rest.py`
(`test_conversions_rest.py`, `test_execution_rest.py`...). Se seguye esa
convencion en vez del nombre literal del ticket."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from tests.integration.composition.conftest import AuthenticatedSession
from tests.unit.composition.factories import build_api_settings

from safent_ads.composition.api import _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.crm.application.webhook_token import hash_webhook_token
from safent_ads.crm.presentation.bridge_rest import build_crm_bridge_router
from safent_ads.iam.presentation.errors import ApiError

pytestmark = pytest.mark.integration

_VALID_DIGEST = "a" * 64


@dataclass(frozen=True, slots=True)
class _Bridge:
    business_id: uuid.UUID
    other_business_id: uuid.UUID
    token: str


async def _make_business(session: AsyncSession, *, slug: str) -> uuid.UUID:
    business_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
            "VALUES (:id, :slug, 'Negocio de prueba', 'Europe/Madrid', 'EUR')"
        ),
        {"id": business_id, "slug": slug},
    )
    return business_id


@pytest.fixture
async def bridge(isolated_database_url: str) -> AsyncIterator[_Bridge]:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    token = f"valid-bridge-token-{uuid.uuid4()}"  # noqa: S105 - fixture, no secreto real
    async with AsyncSession(engine, expire_on_commit=False) as session:
        business_id = await _make_business(session, slug=f"crm-bridge-{uuid.uuid4().hex[:10]}")
        other_business_id = await _make_business(
            session, slug=f"crm-bridge-other-{uuid.uuid4().hex[:10]}"
        )
        await session.execute(
            text(
                "INSERT INTO crm_bridge_tokens (business_id, token_hash) "
                "VALUES (:business_id, :token_hash)"
            ),
            {"business_id": business_id, "token_hash": hash_webhook_token(token)},
        )
        await session.commit()
    try:
        yield _Bridge(business_id=business_id, other_business_id=other_business_id, token=token)
    finally:
        await engine.dispose()


def _app(container: Container) -> FastAPI:
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.include_router(
        build_crm_bridge_router(
            session_factory=container.session_factory,
            clock=container.clock,
        )
    )
    return app


def _client(container: Container, cookies: dict[str, str] | None = None) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=_app(container))
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={**(cookies or {}), "ads_csrf": "test-csrf"},
        headers={"X-CSRF-Token": "test-csrf"},
    )


@pytest.fixture
async def container(isolated_database_url: str) -> AsyncIterator[Container]:
    settings = build_api_settings(database_url=isolated_database_url)
    built = Container.build(settings)
    try:
        yield built
    finally:
        await built.aclose()


async def _seed_customer(client: httpx.AsyncClient, bridge: _Bridge, *, digest: str) -> None:
    response = await client.post(
        "/api/v1/crm/customers",
        headers={"X-Bridge-Token": bridge.token},
        json={
            "business_id": str(bridge.business_id),
            "connector_id": "connector-crm",
            "sync_run_id": str(uuid.uuid4()),
            "items": [
                {
                    "identity_digest": digest,
                    "salt_version": 1,
                    "entity_ref": None,
                    "attribution_rung": "aggregate",
                }
            ],
        },
    )
    assert response.status_code == 202, response.text


class TestIngestCustomers:
    async def test_401_without_a_token(self, container: Container, bridge: _Bridge) -> None:
        async with _client(container) as client:
            response = await client.post(
                "/api/v1/crm/customers",
                json={
                    "business_id": str(bridge.business_id),
                    "connector_id": "connector-crm",
                    "sync_run_id": str(uuid.uuid4()),
                    "items": [],
                },
            )

        assert response.status_code == 401

    async def test_202_with_a_valid_token(self, container: Container, bridge: _Bridge) -> None:
        async with _client(container) as client:
            response = await client.post(
                "/api/v1/crm/customers",
                headers={"X-Bridge-Token": bridge.token},
                json={
                    "business_id": str(bridge.business_id),
                    "connector_id": "connector-crm",
                    "sync_run_id": str(uuid.uuid4()),
                    "items": [
                        {
                            "identity_digest": _VALID_DIGEST,
                            "salt_version": 1,
                            "entity_ref": None,
                            "attribution_rung": "aggregate",
                        }
                    ],
                },
            )

        assert response.status_code == 202
        body = response.json()
        assert body["accepted"] == 1
        assert body["rejected"] == []

    async def test_raw_identifier_is_rejected_with_identity_not_hashed(
        self, container: Container, bridge: _Bridge
    ) -> None:
        async with _client(container) as client:
            response = await client.post(
                "/api/v1/crm/customers",
                headers={"X-Bridge-Token": bridge.token},
                json={
                    "business_id": str(bridge.business_id),
                    "connector_id": "connector-crm",
                    "sync_run_id": str(uuid.uuid4()),
                    "items": [
                        {
                            "identity_digest": "cliente@example.com",
                            "salt_version": 1,
                            "entity_ref": None,
                            "attribution_rung": "aggregate",
                        }
                    ],
                },
            )

        assert response.status_code == 202
        body = response.json()
        assert body["accepted"] == 0
        assert body["rejected"] == [{"index": 0, "code": "IDENTITY_NOT_HASHED"}]
        # ningun `details` de error contiene el identificador crudo
        assert "cliente@example.com" not in response.text


class TestIngestRevenueEvents:
    async def test_positive_refund_is_rejected(self, container: Container, bridge: _Bridge) -> None:
        async with _client(container) as client:
            await _seed_customer(client, bridge, digest=_VALID_DIGEST)
            response = await client.post(
                "/api/v1/crm/revenue-events",
                headers={"X-Bridge-Token": bridge.token},
                json={
                    "business_id": str(bridge.business_id),
                    "connector_id": "connector-crm",
                    "sync_run_id": str(uuid.uuid4()),
                    "items": [
                        {
                            "source_event_id": "evt-refund",
                            "identity_digest": _VALID_DIGEST,
                            "kind": "refund",
                            "amount": {"amount_minor": 500, "currency": "EUR"},
                            "occurred_at": "2026-03-01T00:00:00+00:00",
                            "observed_at": "2026-03-01T00:00:00+00:00",
                            "mapping_version": 1,
                        }
                    ],
                },
            )

        assert response.status_code == 202
        body = response.json()
        assert body["ingested"] == 0
        assert body["rejected"] == [
            {"source_event_id": "evt-refund", "code": "REFUND_MUST_BE_NEGATIVE"}
        ]

    async def test_resending_the_same_batch_is_idempotent(
        self, container: Container, bridge: _Bridge
    ) -> None:
        payload = {
            "business_id": str(bridge.business_id),
            "connector_id": "connector-crm",
            "sync_run_id": str(uuid.uuid4()),
            "items": [
                {
                    "source_event_id": "evt-1",
                    "identity_digest": _VALID_DIGEST,
                    "kind": "first_payment",
                    "amount": {"amount_minor": 10_000, "currency": "EUR"},
                    "occurred_at": "2026-03-01T00:00:00+00:00",
                    "observed_at": "2026-03-01T00:00:00+00:00",
                    "mapping_version": 1,
                }
            ],
        }
        async with _client(container) as client:
            await _seed_customer(client, bridge, digest=_VALID_DIGEST)
            first = await client.post(
                "/api/v1/crm/revenue-events",
                headers={"X-Bridge-Token": bridge.token},
                json=payload,
            )
            second = await client.post(
                "/api/v1/crm/revenue-events",
                headers={"X-Bridge-Token": bridge.token},
                json=payload,
            )

        assert first.json()["ingested"] == 1
        assert second.json()["ingested"] == 0
        assert second.json()["duplicated"] == 1


class TestForgetCustomer:
    async def test_forgetting_deletes_the_customer(
        self, container: Container, bridge: _Bridge
    ) -> None:
        async with _client(container) as client:
            await _seed_customer(client, bridge, digest=_VALID_DIGEST)
            response = await client.post(
                "/api/v1/crm/customers/forget",
                headers={"X-Bridge-Token": bridge.token},
                json={"business_id": str(bridge.business_id), "identity_digest": _VALID_DIGEST},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["recorded"] is True
        assert body["rows_deleted"] >= 1


class TestBridgeHealth:
    async def test_put_bridge_health_records_the_row(
        self, container: Container, bridge: _Bridge
    ) -> None:
        async with _client(container) as client:
            response = await client.put(
                "/api/v1/crm/bridge-health",
                headers={"X-Bridge-Token": bridge.token},
                json={
                    "business_id": str(bridge.business_id),
                    "connector_id": "connector-crm",
                    "connector_state": "degradado",
                    "last_event_at": None,
                    "cause": "credential_expired",
                },
            )

        assert response.status_code == 204


class TestGenerateBridgeToken:
    async def test_428_without_explicit_confirmation(
        self,
        container: Container,
        bridge: _Bridge,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        async with _client(container, authenticated_session.cookies) as client:
            response = await client.post(
                "/api/v1/crm/bridge-token", params={"business_id": str(bridge.business_id)}
            )

        assert response.status_code == 428
        assert response.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
