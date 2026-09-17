"""`/api/v1/calendar-events*` a traves del router real montado sobre
Postgres (`catalog.presentation.rest.build_catalog_router`, vocabulary.md
§4): 201/200/204 sobre `SqlCalendarEventRepository` real, 422 de cada
invariante del contrato, 404 de id ajeno (nunca 403) e IDOR sobre
`business_id` de query -- mismo patron que
`tests/integration/composition/test_idor_sweep_new_routes.py`."""

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

from safent_ads.catalog.presentation.rest import build_catalog_router
from safent_ads.composition.api import _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import AuthenticatedCaller, get_authenticated_caller

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True)
class _TwoBusinesses:
    business_a: uuid.UUID
    business_b: uuid.UUID
    offering_a: uuid.UUID


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
            "INSERT INTO offerings (id, business_id, code, title) "
            "VALUES (:id, :business_id, :code, 'Oferta de prueba')"
        ),
        {"id": offering_id, "business_id": business_id, "code": f"off-{offering_id.hex[:10]}"},
    )
    return offering_id


@pytest.fixture
async def two_businesses(isolated_database_url: str) -> AsyncIterator[_TwoBusinesses]:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        business_a = await _make_business(session, slug=f"cat-a-{uuid.uuid4().hex[:10]}")
        business_b = await _make_business(session, slug=f"cat-b-{uuid.uuid4().hex[:10]}")
        offering_a = await _make_offering(session, business_id=business_a)
        await session.commit()
    try:
        yield _TwoBusinesses(business_a=business_a, business_b=business_b, offering_a=offering_a)
    finally:
        await engine.dispose()


def _app(container: Container) -> FastAPI:
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.include_router(build_catalog_router(container.session_factory, container.clock))
    return app


def _client(container: Container, cookies: dict[str, str]) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=_app(container))
    return httpx.AsyncClient(transport=transport, base_url="http://test", cookies=cookies)


def _restricted_client(
    container: Container, *, allowed_business_id: uuid.UUID, cookies: dict[str, str]
) -> httpx.AsyncClient:
    app = _app(container)

    async def _restricted() -> AuthenticatedCaller:
        return AuthenticatedCaller(allowed_business_ids=frozenset({str(allowed_business_id)}))

    app.dependency_overrides[get_authenticated_caller] = _restricted
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test", cookies=cookies)


def _valid_input(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "Lanzamiento otoño",
        "kind": "launch",
        "window_start": "2026-10-01",
        "window_end": "2026-10-31",
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


class TestCreate:
    async def test_201_and_returns_the_created_event(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        async with _client(container, authenticated_session.cookies) as client:
            response = await client.post(
                "/api/v1/calendar-events",
                params={"business_id": str(two_businesses.business_a)},
                json=_valid_input(offering_id=str(two_businesses.offering_a)),
            )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["business_id"] == str(two_businesses.business_a)
        assert body["offering_id"] == str(two_businesses.offering_a)
        assert body["name"] == "Lanzamiento otoño"
        assert body["kind"] == "launch"
        assert body["region"] is None
        assert body["event_date"] is None
        assert body["is_window_open"] is False

    async def test_422_when_window_start_is_not_before_window_end(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        async with _client(container, authenticated_session.cookies) as client:
            response = await client.post(
                "/api/v1/calendar-events",
                params={"business_id": str(two_businesses.business_a)},
                json=_valid_input(window_start="2026-10-31", window_end="2026-10-01"),
            )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_422_when_event_date_is_before_window_start(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        async with _client(container, authenticated_session.cookies) as client:
            response = await client.post(
                "/api/v1/calendar-events",
                params={"business_id": str(two_businesses.business_a)},
                json=_valid_input(event_date="2026-09-01"),
            )

        assert response.status_code == 422

    async def test_422_when_kind_is_invalid(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        async with _client(container, authenticated_session.cookies) as client:
            response = await client.post(
                "/api/v1/calendar-events",
                params={"business_id": str(two_businesses.business_a)},
                json=_valid_input(kind="not_a_kind"),
            )

        assert response.status_code == 422

    async def test_422_when_name_is_too_long(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        async with _client(container, authenticated_session.cookies) as client:
            response = await client.post(
                "/api/v1/calendar-events",
                params={"business_id": str(two_businesses.business_a)},
                json=_valid_input(name="x" * 121),
            )

        assert response.status_code == 422

    async def test_422_when_region_is_too_short(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        async with _client(container, authenticated_session.cookies) as client:
            response = await client.post(
                "/api/v1/calendar-events",
                params={"business_id": str(two_businesses.business_a)},
                json=_valid_input(region="a"),
            )

        assert response.status_code == 422

    async def test_422_when_offering_id_does_not_exist(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        async with _client(container, authenticated_session.cookies) as client:
            response = await client.post(
                "/api/v1/calendar-events",
                params={"business_id": str(two_businesses.business_a)},
                json=_valid_input(offering_id=str(uuid.uuid4())),
            )

        assert response.status_code == 422

    async def test_404_when_the_caller_cannot_access_business_id(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        async with _restricted_client(
            container,
            allowed_business_id=two_businesses.business_a,
            cookies=authenticated_session.cookies,
        ) as client:
            response = await client.post(
                "/api/v1/calendar-events",
                params={"business_id": str(two_businesses.business_b)},
                json=_valid_input(),
            )

        assert response.status_code == 404


class TestListAndGet:
    async def test_list_returns_the_created_event(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        async with _client(container, authenticated_session.cookies) as client:
            created = await client.post(
                "/api/v1/calendar-events",
                params={"business_id": str(two_businesses.business_a)},
                json=_valid_input(),
            )
            assert created.status_code == 201, created.text

            response = await client.get(
                "/api/v1/calendar-events", params={"business_id": str(two_businesses.business_a)}
            )

        assert response.status_code == 200
        ids = {item["calendar_event_id"] for item in response.json()["items"]}
        assert created.json()["calendar_event_id"] in ids

    async def test_open_only_excludes_a_closed_window(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        async with _client(container, authenticated_session.cookies) as client:
            closed = await client.post(
                "/api/v1/calendar-events",
                params={"business_id": str(two_businesses.business_a)},
                json=_valid_input(window_start="2020-01-01", window_end="2020-02-01"),
            )
            assert closed.status_code == 201, closed.text

            response = await client.get(
                "/api/v1/calendar-events",
                params={"business_id": str(two_businesses.business_a), "open_only": "true"},
            )

        assert response.status_code == 200
        ids = {item["calendar_event_id"] for item in response.json()["items"]}
        assert closed.json()["calendar_event_id"] not in ids


class TestUpdate:
    async def test_200_and_persists_the_change(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        async with _client(container, authenticated_session.cookies) as client:
            created = await client.post(
                "/api/v1/calendar-events",
                params={"business_id": str(two_businesses.business_a)},
                json=_valid_input(),
            )
            calendar_event_id = created.json()["calendar_event_id"]

            response = await client.put(
                f"/api/v1/calendar-events/{calendar_event_id}",
                params={"business_id": str(two_businesses.business_a)},
                json=_valid_input(name="Lanzamiento invierno", kind="promotion"),
            )

        assert response.status_code == 200, response.text
        assert response.json()["name"] == "Lanzamiento invierno"
        assert response.json()["kind"] == "promotion"

    async def test_404_when_the_event_belongs_to_another_business(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        async with _client(container, authenticated_session.cookies) as client:
            created = await client.post(
                "/api/v1/calendar-events",
                params={"business_id": str(two_businesses.business_b)},
                json=_valid_input(),
            )
            calendar_event_id = created.json()["calendar_event_id"]

            response = await client.put(
                f"/api/v1/calendar-events/{calendar_event_id}",
                params={"business_id": str(two_businesses.business_a)},
                json=_valid_input(name="No deberia poder"),
            )

        assert response.status_code == 404


class TestDelete:
    async def test_204_and_the_event_is_gone(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        async with _client(container, authenticated_session.cookies) as client:
            created = await client.post(
                "/api/v1/calendar-events",
                params={"business_id": str(two_businesses.business_a)},
                json=_valid_input(),
            )
            calendar_event_id = created.json()["calendar_event_id"]

            response = await client.delete(
                f"/api/v1/calendar-events/{calendar_event_id}",
                params={"business_id": str(two_businesses.business_a)},
            )
            assert response.status_code == 204

            listing = await client.get(
                "/api/v1/calendar-events", params={"business_id": str(two_businesses.business_a)}
            )
        ids = {item["calendar_event_id"] for item in listing.json()["items"]}
        assert calendar_event_id not in ids

    async def test_404_when_the_event_belongs_to_another_business(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        async with _client(container, authenticated_session.cookies) as client:
            created = await client.post(
                "/api/v1/calendar-events",
                params={"business_id": str(two_businesses.business_b)},
                json=_valid_input(),
            )
            calendar_event_id = created.json()["calendar_event_id"]

            response = await client.delete(
                f"/api/v1/calendar-events/{calendar_event_id}",
                params={"business_id": str(two_businesses.business_a)},
            )

        assert response.status_code == 404

    async def test_404_for_an_unknown_id(
        self,
        container: Container,
        two_businesses: _TwoBusinesses,
        authenticated_session: AuthenticatedSession,
    ) -> None:
        async with _client(container, authenticated_session.cookies) as client:
            response = await client.delete(
                f"/api/v1/calendar-events/{uuid.uuid4()}",
                params={"business_id": str(two_businesses.business_a)},
            )

        assert response.status_code == 404
