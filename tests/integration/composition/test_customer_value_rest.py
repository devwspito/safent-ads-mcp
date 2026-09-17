"""`GET /api/v1/economics/customer-value` a traves del router real sobre
Postgres (spec 027 T017, contracts/crm-link.md §3): sin volumen suficiente
declara `no_number_reason`, nunca un cero fabricado."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from tests.integration.composition.conftest import AuthenticatedSession
from tests.unit.composition.factories import build_api_settings

from safent_ads.composition.container import Container
from safent_ads.economics.presentation.customer_value_rest import build_customer_value_router

pytestmark = pytest.mark.integration


@pytest.fixture
async def business_id(isolated_database_url: str) -> AsyncIterator[uuid.UUID]:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    new_business_id = uuid.uuid4()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await session.execute(
            text(
                "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
                "VALUES (:id, :slug, 'Negocio de prueba', 'Europe/Madrid', 'EUR')"
            ),
            {"id": new_business_id, "slug": f"customer-value-{new_business_id.hex[:10]}"},
        )
        await session.commit()
    try:
        yield new_business_id
    finally:
        await engine.dispose()


def _app(container: Container) -> FastAPI:
    app = FastAPI()
    app.state.container = container
    app.include_router(build_customer_value_router(container.session_factory))
    return app


@pytest.fixture
async def container(isolated_database_url: str) -> AsyncIterator[Container]:
    settings = build_api_settings(database_url=isolated_database_url)
    built = Container.build(settings)
    try:
        yield built
    finally:
        await built.aclose()


async def test_returns_no_number_reason_without_enough_customers(
    container: Container, business_id: uuid.UUID, authenticated_session: AuthenticatedSession
) -> None:
    transport = httpx.ASGITransport(app=_app(container))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", cookies=authenticated_session.cookies
    ) as client:
        response = await client.get(
            "/api/v1/economics/customer-value", params={"business_id": str(business_id)}
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_provisional"] is True
    assert body["projected_contribution_minor"] is None
    assert body["no_number_reason"]
