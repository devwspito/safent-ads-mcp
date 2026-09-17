"""Journey: `connect_platform_account` in single-owner mode (`ADS_SINGLE_OWNER_MODE`).

2026-09-16: the hosted MCP presents the owner's static token as
`caller_id="owner"` (the real `SingleOwnerCallerScopeResolver`, not a person
seat) and `connect_platform_account` refused it with "sin puesto". The
connection is now attributed to the installation's sole `owners` row and
anything else fails closed. Own migrated database (reusing the session
helpers, never re-derived): "sole owner" and the UNIQUE `state` of
`oauth_connect_sessions` are global facts that rows from other journeys would
silently change."""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from testcontainers.postgres import PostgresContainer

from safent_ads.composition.container import Container
from safent_ads.mcp.infrastructure.rate_limiter import InMemoryQuota
from safent_ads.mcp.infrastructure.single_owner_caller_scope_resolver import (
    SingleOwnerCallerScopeResolver,
)
from safent_ads.mcp.presentation.dispatcher import ToolDispatcher
from safent_ads.shared.clock import SystemClock
from tests.conftest import _recreate_database, alembic_upgrade, to_alembic_dsn, with_database
from tests.e2e.journeys.conftest import build_api_settings, mcp_session_over_registry
from tests.e2e.journeys.test_journey_accounts import _connection_only_registry
from tests.unit.accounts.application.conftest import FakeOAuthBrokerPort, default_begin_result

pytestmark = pytest.mark.integration

_TOKEN = "owner-static-token"  # noqa: S105 - fixture bearer, never a real credential
_DATABASE = "ads_owner_connect"


@pytest.fixture(scope="module")
def owner_database_url(postgres_container: PostgresContainer) -> str:
    base_url = postgres_container.get_connection_url()
    _recreate_database(base_url, _DATABASE)
    url = with_database(to_alembic_dsn(base_url), _DATABASE)
    alembic_upgrade(url)
    return url


@pytest.fixture
async def owner_container(owner_database_url: str) -> AsyncIterator[Container]:
    built = Container.build(build_api_settings(database_url=owner_database_url))
    try:
        yield built
    finally:
        await built.aclose()


@pytest.fixture(scope="module")
def business_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
async def seeded_business(owner_container: Container, business_id: uuid.UUID) -> uuid.UUID:
    async with owner_container.session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
                "VALUES (:id, :slug, 'Negocio del dueño', 'Europe/Madrid', 'EUR') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": business_id, "slug": f"own-{business_id.hex[:10]}"},
        )
        await session.commit()
    return business_id


async def _insert_owner(container: Container) -> uuid.UUID:
    owner_id = uuid.uuid4()
    async with container.session_factory() as session:
        await session.execute(
            text("INSERT INTO owners (id, email, password_hash) VALUES (:id, :email, 'x')"),
            {"id": owner_id, "email": f"o-{owner_id.hex[:8]}@x.example"},
        )
        await session.commit()
    return owner_id


def _unique_begin_result():
    state = uuid.uuid4().hex
    base = default_begin_result()
    return dataclasses.replace(
        base, state=state, authorization_url=f"https://accounts.google.com/auth?state={state}"
    )


async def _connect(container: Container, business_id: uuid.UUID, platform: str):
    oauth_broker = FakeOAuthBrokerPort(begin_result=_unique_begin_result())
    registry = _connection_only_registry(container, oauth_broker)
    dispatcher = ToolDispatcher(registry=registry, quota=InMemoryQuota(clock=SystemClock()))
    resolver = SingleOwnerCallerScopeResolver(container.session_factory, expected_token=_TOKEN)
    async with mcp_session_over_registry(registry, dispatcher, resolver, token=_TOKEN) as session:
        return await session.call_tool(
            "connect_platform_account",
            {"args": {"business_id": str(business_id), "platform": platform}},
        )


async def _connect_sessions(container: Container) -> int:
    async with container.session_factory() as session:
        return (
            await session.execute(text("SELECT count(*) FROM oauth_connect_sessions"))
        ).scalar_one()


async def test_the_sole_owner_connects_and_the_session_is_attributed_to_it(
    owner_container: Container, seeded_business: uuid.UUID
) -> None:
    owner_id = await _insert_owner(owner_container)

    reply = await _connect(owner_container, seeded_business, "meta")

    assert reply.is_error is False, reply.content
    result = reply.structured_content["result"]
    assert result["authorize_url"].startswith("https://accounts.google.com/auth?state=")
    async with owner_container.session_factory() as session:
        stored = (
            await session.execute(
                text("SELECT owner_id FROM oauth_connect_sessions WHERE id = :id"),
                {"id": result["session_id"]},
            )
        ).scalar_one()
    assert str(stored) == str(owner_id)


async def test_without_exactly_one_owner_it_fails_closed_and_stores_nothing(
    owner_container: Container, seeded_business: uuid.UUID
) -> None:
    """A second owner row appears (or none existed): the tool must not guess an
    initiator nor write a connect session -- the clean `VALIDATION_ERROR`
    envelope and no new row."""
    before = await _connect_sessions(owner_container)
    await _insert_owner(owner_container)

    reply = await _connect(owner_container, seeded_business, "google")

    assert reply.is_error is False
    assert reply.structured_content["error"]["code"] == "VALIDATION_ERROR"
    assert await _connect_sessions(owner_container) == before
