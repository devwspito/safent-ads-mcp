"""Journey 4: `connect_platform_account` over the real MCP HTTP transport.
`aprobar` reaches the real `BeginOAuthConnect` use case (a real
`oauth_connect_sessions` row, real FKs) with only the `OAuthBrokerPort`
socket seam doubled (`tests/unit/accounts/application/conftest.py::
FakeOAuthBrokerPort`, reused rather than re-derived) -- "mock only at
architectural seams, never internal collaborators". `ver` cannot reach it
at all (same mount-time exclusion documented in `test_journey_permissions.
py`, not re-explained here)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from safent_ads.composition.container import Container
from safent_ads.mcp.application.caller_scope import Permission
from safent_ads.mcp.infrastructure.rate_limiter import InMemoryQuota
from safent_ads.mcp.presentation.catalog import build_default_registry
from safent_ads.mcp.presentation.connection_tools import ConnectionToolServices
from safent_ads.mcp.presentation.dispatcher import ToolDispatcher
from safent_ads.mcp.presentation.native_ads_tools import NativeAdsToolServices
from safent_ads.shared.clock import SystemClock
from safent_ads.shared.ids import UuidIdGenerator
from tests.e2e.journeys.conftest import (
    BASE_URL,
    _FixedScopeResolver,
    mcp_session,
    mcp_session_over_registry,
    person_scope,
)
from tests.unit.accounts.application.conftest import FakeOAuthBrokerPort, default_begin_result
from tests.unit.bundle.test_mcp_registry_matches_overlay_and_contract import (
    _creative_generation_services,
    _economics_service,
    _experiment_services,
    _FakeProposalWritePort,
    _opportunity_services,
    _optimization_service,
    _read_model_ports,
    _search_terms_services,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def business_id(container: Container) -> uuid.UUID:
    business_id = uuid.uuid4()
    async with container.session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
                "VALUES (:id, :slug, 'Negocio de cuentas', 'Europe/Madrid', 'EUR')"
            ),
            {"id": business_id, "slug": f"acct-{business_id.hex[:10]}"},
        )
        await session.commit()
    return business_id


def _connection_only_registry(container: Container, oauth_broker: FakeOAuthBrokerPort):
    connection_services = ConnectionToolServices(
        session_factory=container.session_factory,
        oauth_broker=oauth_broker,
        id_generator=UuidIdGenerator(),
        public_base_url=BASE_URL,
    )
    return build_default_registry(
        _read_model_ports(),
        SystemClock(),
        _FakeProposalWritePort(),
        experiment_services=_experiment_services(),
        search_terms_services=_search_terms_services(),
        opportunity_services=_opportunity_services(),
        economics_service=_economics_service(),
        optimization_service=_optimization_service(),
        creative_generation_services=_creative_generation_services(),
        native_ads_services=NativeAdsToolServices(AsyncMock()),
        offering_creation=AsyncMock(),
        campaign_drafts=AsyncMock(),
        connection_services=connection_services,
    )


async def test_ver_cannot_reach_connect_platform_account(
    container: Container, business_id: uuid.UUID
) -> None:
    """`connect_platform_account` is `CONNECTION_WRITE`, mounted only for
    `aprobar` -- `ver` gets `mount.py`'s `PERMISSION_DENIED` gate (bug 1,
    hotfix 0.2.20), same convention as any other `ToolDispatchError`
    (`is_error=False`, `test_journey_permissions.py` covers the audit row)."""
    resolver = _FixedScopeResolver(person_scope(Permission.VIEW, business_id=business_id))
    async with mcp_session(container, resolver) as session:
        reply = await session.call_tool(
            "connect_platform_account",
            {"args": {"business_id": str(business_id), "platform": "google"}},
        )
    assert reply.is_error is False
    assert reply.structured_content["error"]["code"] == "PERMISSION_DENIED"


async def test_aprobar_connects_a_platform_account_on_the_configured_host(
    container: Container, business_id: uuid.UUID
) -> None:
    """The tool builds `redirect_uri` from `ConnectionToolServices.public_base_url`
    (config), never from the request's `Host` header -- `SeatCredentialRouter`
    already 403s any `Host` outside that same base (`_host_allowed`), so a
    forged header never reaches this far either way."""
    # `oauth_connect_sessions.owner_id` FKs to `owners`: the seat person
    # must be a real owner row, not just a bearer/`CallerScope` claim.
    person_id = uuid.uuid4()
    async with container.session_factory() as session:
        await session.execute(
            text("INSERT INTO owners (id, email, password_hash) VALUES (:id, :email, 'x')"),
            {"id": person_id, "email": f"o-{person_id.hex[:8]}@x.example"},
        )
        await session.commit()

    oauth_broker = FakeOAuthBrokerPort(begin_result=default_begin_result())
    registry = _connection_only_registry(container, oauth_broker)
    dispatcher = ToolDispatcher(registry=registry, quota=InMemoryQuota(clock=SystemClock()))
    resolver = _FixedScopeResolver(
        person_scope(Permission.APPROVE, business_id=business_id, person_id=person_id)
    )
    async with mcp_session_over_registry(registry, dispatcher, resolver) as session:
        reply = await session.call_tool(
            "connect_platform_account",
            {"args": {"business_id": str(business_id), "platform": "google"}},
        )

    assert reply.is_error is False, reply.content
    result = reply.structured_content["result"]
    assert result["authorize_url"] == default_begin_result().authorization_url
    async with container.session_factory() as session:
        stored = (
            await session.execute(
                text(
                    "SELECT owner_id FROM oauth_connect_sessions "
                    "WHERE id = :id AND business_id = :business"
                ),
                {"id": result["session_id"], "business": business_id},
            )
        ).scalar_one()
    assert str(stored) == str(person_id)
