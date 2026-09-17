"""`/api/v1/conversions*` a traves del router real sobre Postgres
(`crm.presentation.rest.build_conversions_router`, contracts/rest-api.md
§Conversiones, T220): import CSV, IDOR sobre `business_id` de query, y
autenticacion del webhook por `X-Webhook-Token` (401 sin/ con token
invalido, 202 con uno valido) -- mismo patron que `test_catalog_rest.py`/
`test_idor_sweep_new_routes.py`."""

from __future__ import annotations

import io
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
from safent_ads.crm.infrastructure.identity_salt import HkdfIdentitySalt
from safent_ads.crm.presentation.rest import build_conversions_router
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import AuthenticatedCaller, get_authenticated_caller

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True)
class _TwoBusinesses:
    business_a: uuid.UUID
    business_b: uuid.UUID
    webhook_token: str


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
async def two_businesses(isolated_database_url: str) -> AsyncIterator[_TwoBusinesses]:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    # `token_hash` es UNIQUE a nivel global (0029_economics_inputs): un
    # literal fijo chocaria entre negocios de tests distintos sobre la
    # misma base compartida de `isolated_database_url` (session-scoped).
    webhook_token = f"valid-webhook-token-{uuid.uuid4()}"  # noqa: S105 - fixture, no secreto real
    async with AsyncSession(engine, expire_on_commit=False) as session:
        business_a = await _make_business(session, slug=f"conv-a-{uuid.uuid4().hex[:10]}")
        business_b = await _make_business(session, slug=f"conv-b-{uuid.uuid4().hex[:10]}")
        await session.execute(
            text(
                "INSERT INTO conversion_webhook_tokens (business_id, token_hash) "
                "VALUES (:business_id, :token_hash)"
            ),
            {"business_id": business_a, "token_hash": hash_webhook_token(webhook_token)},
        )
        await session.commit()
    try:
        yield _TwoBusinesses(
            business_a=business_a, business_b=business_b, webhook_token=webhook_token
        )
    finally:
        await engine.dispose()


def _app(container: Container) -> FastAPI:
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.include_router(
        build_conversions_router(
            session_factory=container.session_factory,
            identity_salt=HkdfIdentitySalt(b"0" * 32),
            clock=container.clock,
        )
    )
    return app


def _client(container: Container, cookies: dict[str, str]) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=_app(container))
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={**cookies, "ads_csrf": "test-csrf"},
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


class TestImportConversions:
    async def test_200_imports_a_valid_csv(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        csv_bytes = b"occurred_at,kind,email\n2026-03-01,lead,a@x.com\n"
        async with _client(container, authenticated_session.cookies) as client:
            response = await client.post(
                "/api/v1/conversions/import",
                params={"business_id": str(two_businesses.business_a)},
                files={"file": ("conversions.csv", io.BytesIO(csv_bytes), "text/csv")},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["imported"] == 1
        assert body["duplicates"] == 0
        assert body["rejected"] == []

    async def test_422_when_required_columns_are_missing(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        csv_bytes = b"email\na@x.com\n"
        async with _client(container, authenticated_session.cookies) as client:
            response = await client.post(
                "/api/v1/conversions/import",
                params={"business_id": str(two_businesses.business_a)},
                files={"file": ("conversions.csv", io.BytesIO(csv_bytes), "text/csv")},
            )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_404_when_caller_lacks_business_access(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        app = _app(container)

        async def _restricted() -> AuthenticatedCaller:
            return AuthenticatedCaller(allowed_business_ids=frozenset({str(uuid.uuid4())}))

        app.dependency_overrides[get_authenticated_caller] = _restricted
        transport = httpx.ASGITransport(app=app)
        csv_bytes = b"occurred_at,kind,email\n2026-03-01,lead,a@x.com\n"
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=authenticated_session.cookies
        ) as client:
            response = await client.post(
                "/api/v1/conversions/import",
                params={"business_id": str(two_businesses.business_a)},
                files={"file": ("conversions.csv", io.BytesIO(csv_bytes), "text/csv")},
            )

        assert response.status_code == 404


class TestWebhook:
    async def test_401_without_a_token(
        self, container: Container, two_businesses: _TwoBusinesses
    ) -> None:
        del two_businesses
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app(container)), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/conversions/webhook",
                json={"occurred_at": "2026-03-01", "kind": "lead", "email": "a@x.com"},
            )

        assert response.status_code == 401

    async def test_401_with_an_unknown_token(
        self, container: Container, two_businesses: _TwoBusinesses
    ) -> None:
        del two_businesses
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app(container)), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/conversions/webhook",
                headers={"X-Webhook-Token": "not-the-right-token"},
                json={"occurred_at": "2026-03-01", "kind": "lead", "email": "a@x.com"},
            )

        assert response.status_code == 401

    async def test_202_with_a_valid_token(
        self, container: Container, two_businesses: _TwoBusinesses
    ) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app(container)), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/conversions/webhook",
                headers={"X-Webhook-Token": two_businesses.webhook_token},
                json={"occurred_at": "2026-03-01", "kind": "lead", "email": "a@x.com"},
            )

        assert response.status_code == 202
        assert response.json() == {"status": "accepted"}

    async def test_422_with_an_unknown_kind(
        self, container: Container, two_businesses: _TwoBusinesses
    ) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app(container)), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/conversions/webhook",
                headers={"X-Webhook-Token": two_businesses.webhook_token},
                json={"occurred_at": "2026-03-01", "kind": "not_a_kind", "email": "a@x.com"},
            )

        assert response.status_code == 422


class TestGenerateWebhookToken:
    async def test_428_without_explicit_confirmation(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        async with _client(container, authenticated_session.cookies) as client:
            response = await client.post(
                "/api/v1/conversions/webhook-token",
                params={"business_id": str(two_businesses.business_a)},
            )

        assert response.status_code == 428
        assert response.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
