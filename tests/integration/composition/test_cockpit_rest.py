"""`GET /api/v1/cockpit` de extremo a extremo (026, tasks.md T007): sesion
real, `ETag`/`If-None-Match` -> `304`, y 401/404 en los bordes de
`RequireBusinessAccess` (mismo patron que
`tests/integration/composition/test_execution_read_rest.py`)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.integration.composition.conftest import AuthenticatedSession
from tests.unit.composition.factories import build_api_settings

from safent_ads.composition.app import create_app

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _client(app: FastAPI, cookies: dict[str, str]) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test", cookies=cookies
    ) as client:
        yield client


@pytest.fixture
async def seeded_business(isolated_database_url: str) -> AsyncIterator[uuid.UUID]:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    business_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
                "VALUES (:id, :slug, 'Negocio cockpit REST', 'Europe/Madrid', 'EUR')"
            ),
            {"id": str(business_id), "slug": f"cockpit-rest-{business_id.hex[:10]}"},
        )
    try:
        yield business_id
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM businesses WHERE id = :id"), {"id": str(business_id)}
            )
        await engine.dispose()


async def test_get_cockpit_returns_the_expected_shape_with_an_etag(
    isolated_database_url: str,
    authenticated_session: AuthenticatedSession,
    seeded_business: uuid.UUID,
) -> None:
    app = create_app(build_api_settings(database_url=isolated_database_url))

    async with _client(app, authenticated_session.cookies) as client:
        response = await client.get(
            "/api/v1/cockpit", params={"business_id": str(seeded_business)}
        )

    assert response.status_code == 200
    assert response.headers["etag"]
    assert response.headers["cache-control"] == "private, no-store"
    body = response.json()
    assert body["business_id"] == str(seeded_business)
    assert body["window"] == "7d"
    assert body["rows"] == []
    assert body["header"]["customers"]["today"]["status"] == "no_customer_source"
    assert body["changes_since"]["is_partial"] is True


async def test_repeating_if_none_match_returns_304(
    isolated_database_url: str,
    authenticated_session: AuthenticatedSession,
    seeded_business: uuid.UUID,
) -> None:
    app = create_app(build_api_settings(database_url=isolated_database_url))

    async with _client(app, authenticated_session.cookies) as client:
        first = await client.get(
            "/api/v1/cockpit", params={"business_id": str(seeded_business)}
        )
        etag = first.headers["etag"]
        second = await client.get(
            "/api/v1/cockpit",
            params={"business_id": str(seeded_business)},
            headers={"if-none-match": etag},
        )

    assert second.status_code == 304


async def test_get_cockpit_without_a_session_is_unauthorized(
    isolated_database_url: str, seeded_business: uuid.UUID
) -> None:
    app = create_app(build_api_settings(database_url=isolated_database_url))

    async with _client(app, {}) as client:
        response = await client.get(
            "/api/v1/cockpit", params={"business_id": str(seeded_business)}
        )

    assert response.status_code == 401


async def test_get_cockpit_for_an_unknown_business_is_404_not_403(
    isolated_database_url: str, authenticated_session: AuthenticatedSession
) -> None:
    app = create_app(build_api_settings(database_url=isolated_database_url))

    async with _client(app, authenticated_session.cookies) as client:
        response = await client.get(
            "/api/v1/cockpit", params={"business_id": str(uuid.uuid4())}
        )

    assert response.status_code == 404
