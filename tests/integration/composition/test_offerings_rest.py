"""`/api/v1/offerings*` a traves del router real sobre Postgres
(`economics.presentation.offerings_rest.build_offerings_router`,
contracts/rest-api.md §Economia unitaria, T131/T132): 200/422 de cada
invariante, y 404 de oferta ajena (nunca 403) -- mismo patron que
`test_catalog_rest.py`/`test_idor_sweep_new_routes.py`."""

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
from safent_ads.economics.presentation.offerings_rest import build_offerings_router
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import AuthenticatedCaller, get_authenticated_caller

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True)
class _TwoBusinesses:
    business_a: uuid.UUID
    business_b: uuid.UUID
    offering_a: uuid.UUID
    offering_b: uuid.UUID


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


async def _make_offering(session: AsyncSession, *, business_id: uuid.UUID) -> uuid.UUID:
    offering_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO offerings (id, business_id, code, title, price_amount, price_currency) "
            "VALUES (:id, :business_id, :code, 'Oferta de prueba', 1200, 'EUR')"
        ),
        {"id": offering_id, "business_id": business_id, "code": f"off-{offering_id.hex[:10]}"},
    )
    return offering_id


@pytest.fixture
async def two_businesses(isolated_database_url: str) -> AsyncIterator[_TwoBusinesses]:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        business_a = await _make_business(session, slug=f"off-a-{uuid.uuid4().hex[:10]}")
        business_b = await _make_business(session, slug=f"off-b-{uuid.uuid4().hex[:10]}")
        offering_a = await _make_offering(session, business_id=business_a)
        offering_b = await _make_offering(session, business_id=business_b)
        await session.commit()
    try:
        yield _TwoBusinesses(
            business_a=business_a,
            business_b=business_b,
            offering_a=offering_a,
            offering_b=offering_b,
        )
    finally:
        await engine.dispose()


def _app(container: Container) -> FastAPI:
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.include_router(build_offerings_router(container.session_factory))
    return app


def _client(container: Container, cookies: dict[str, str]) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=_app(container))
    return httpx.AsyncClient(transport=transport, base_url="http://test", cookies=cookies)


def _valid_economics(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "vat_rate_pct": "21",
        "delivery_cost_minor": 9_000,
        "sales_cost_minor": 14_000,
        "refund_rate_pct": "6",
        "payment_plan": "none",
        "currency": "EUR",
    }
    body.update(overrides)
    return body


@pytest.fixture
async def container(isolated_database_url: str) -> AsyncIterator[Container]:
    settings = build_api_settings(database_url=isolated_database_url)
    built = Container.build(settings)
    try:
        yield built
    finally:
        await built.aclose()


class TestListOfferings:
    async def test_200_lists_offerings_with_null_economics_when_unfilled(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        async with _client(container, authenticated_session.cookies) as client:
            response = await client.get(
                "/api/v1/offerings", params={"business_id": str(two_businesses.business_a)}
            )

        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert len(items) == 1
        assert items[0]["offering_id"] == str(two_businesses.offering_a)
        assert items[0]["economics"] is None

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
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=authenticated_session.cookies
        ) as client:
            response = await client.get(
                "/api/v1/offerings", params={"business_id": str(two_businesses.business_a)}
            )

        assert response.status_code == 404


class TestPutOfferingEconomics:
    async def test_200_persists_economics_and_shows_up_on_the_list(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        async with _client(container, authenticated_session.cookies) as client:
            put_response = await client.put(
                f"/api/v1/offerings/{two_businesses.offering_a}/economics",
                params={"business_id": str(two_businesses.business_a)},
                json=_valid_economics(),
            )
            list_response = await client.get(
                "/api/v1/offerings", params={"business_id": str(two_businesses.business_a)}
            )

        assert put_response.status_code == 200, put_response.text
        assert put_response.json()["vat_rate_pct"] == 21.0
        assert list_response.json()["items"][0]["economics"]["vat_rate_pct"] == 21.0

    @pytest.mark.parametrize(
        "overrides",
        [
            {"vat_rate_pct": "-1"},
            {"vat_rate_pct": "150"},
            {"refund_rate_pct": "101"},
            {"delivery_cost_minor": -1},
            {"sales_cost_minor": -1},
        ],
    )
    async def test_422_on_out_of_range_values(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
        overrides: dict[str, object],
    ) -> None:
        async with _client(container, authenticated_session.cookies) as client:
            response = await client.put(
                f"/api/v1/offerings/{two_businesses.offering_a}/economics",
                params={"business_id": str(two_businesses.business_a)},
                json=_valid_economics(**overrides),
            )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_422_when_a_required_field_is_missing(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        body = _valid_economics()
        del body["vat_rate_pct"]
        async with _client(container, authenticated_session.cookies) as client:
            response = await client.put(
                f"/api/v1/offerings/{two_businesses.offering_a}/economics",
                params={"business_id": str(two_businesses.business_a)},
                json=body,
            )

        assert response.status_code == 422

    async def test_404_when_offering_belongs_to_another_business(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        """`business_id` de query es accesible (business_a existe), pero
        `offering_b` es de business_b -- 404 lo pone el caso de uso, no la
        dependencia de acceso (segunda capa de defensa IDOR)."""
        async with _client(container, authenticated_session.cookies) as client:
            response = await client.put(
                f"/api/v1/offerings/{two_businesses.offering_b}/economics",
                params={"business_id": str(two_businesses.business_a)},
                json=_valid_economics(),
            )

        assert response.status_code == 404

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
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=authenticated_session.cookies
        ) as client:
            response = await client.put(
                f"/api/v1/offerings/{two_businesses.offering_a}/economics",
                params={"business_id": str(two_businesses.business_a)},
                json=_valid_economics(),
            )

        assert response.status_code == 404
