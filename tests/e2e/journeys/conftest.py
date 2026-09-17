"""Shared harness for the `journeys/` bank (companion 0.2.19 regression
safety net, hotfix/0.2.20): a real `Container` over `isolated_database_url`
(testcontainers Postgres) and the REAL MCP HTTP transport
(`mcp/presentation/http.py::build_mcp_asgi_apps`, `mount.py::mount_tools`)
driven by the SDK's own `ClientSession` -- same pattern proven in
`tests/integration/mcp/test_campaign_creation_plan.py`, not a hand-rolled
JSON-RPC client.

Every journey needs a real Postgres: `_build_mcp_registry_and_dispatcher`
(reused, not re-derived, from `composition/app.py`) wires SQL-backed read
ports and `_SqlDecisionAudit` writes one `decision_log` row per dispatched
call, success or failure (`dispatcher.py` docstring). All five journey
files are `pytest.mark.integration`; run with `make test-integration`
(see module docstrings for why `make test` -- `pytest -m "not
integration"` -- cannot carry them: every tool call here, even a denied
one that actually reaches the dispatcher, commits an audit row)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

from safent_ads.composition.app import _build_mcp_registry_and_dispatcher
from safent_ads.composition.container import Container
from safent_ads.mcp.application.caller_scope import CallerScope, CallerScopeResolverPort, Permission
from safent_ads.mcp.application.seat_authority import SeatAuthorityDeniedError
from safent_ads.mcp.presentation.catalog import registries_by_permission
from safent_ads.mcp.presentation.dispatcher import ToolDispatcher
from safent_ads.mcp.presentation.http import build_mcp_asgi_apps, build_mcp_servers
from safent_ads.mcp.presentation.registry import ToolRegistry
from tests.unit.composition.factories import build_api_settings

# Pinned split (`tests/unit/mcp/presentation/test_catalog_registries_by_permission.py`,
# contracts/mcp.md §3): 74 READ visible to `ver` (kit_services +3, Cloudflare
# +2, both landed on this branch since this split was last counted here);
# +19 PROPOSAL (20 pinned there minus 1 -- `propose_campaign_package` is
# absent because `build_api_settings()` here leaves
# `campaign_packages_enabled` at its default `False`, `composition/app.py`)
# +3 CATALOG_WRITE (Cloudflare `upsert_dns_record`/`delete_dns_record`)
# +1 CREATIVE_WRITE = 97 for `proponer`; +2 CONNECTION_WRITE = 99 for
# `aprobar` (also what single-owner mode serves).
READ_TOOL_COUNT = 75
PROPOSE_TOOL_COUNT = 98
APPROVE_TOOL_COUNT = 100

BASE_URL = "https://ads.journeys.test"
_TOKEN = "synthetic-journey-token"  # noqa: S105 - fixture bearer, never a real credential


@pytest.fixture
async def container(isolated_database_url: str) -> AsyncIterator[Container]:
    settings = build_api_settings(database_url=isolated_database_url)
    built = Container.build(settings)
    try:
        yield built
    finally:
        await built.aclose()


def person_scope(
    permission: Permission, *, business_id: object, person_id: uuid.UUID | None = None,
    label: str = "Agente QA",
) -> CallerScope:
    """`caller_id="person:<uuid>"` (`EnterpriseSeatCallerScopeResolver`'s own
    format, `mcp/infrastructure/enterprise_seat_caller_scope_resolver.py`):
    a real person seat, not the `owner`/service caller ids other fixtures
    use, so audit rows are attributable to a person as the task requires."""
    return CallerScope(
        f"person:{person_id or uuid.uuid4()}", frozenset({str(business_id)}), permission, label
    )


class _FixedScopeResolver:
    """Substitutes Enterprise (`mcp/testing/fakes.py::FakeSeatAuthority`
    pattern) with a single fixed scope per bearer -- the seam this app
    factory is designed to swap (`composition/app.py::create_app`'s
    `caller_scope_resolver=` parameter)."""

    def __init__(self, scope: CallerScope, *, token: str = _TOKEN) -> None:
        self._scope, self._token = scope, token

    async def resolve(self, bearer_token: str) -> CallerScope:
        if bearer_token != self._token:
            raise SeatAuthorityDeniedError("ads_seat_invalid")
        return self._scope


@asynccontextmanager
async def mcp_session_over_registry(
    registry: ToolRegistry,
    dispatcher: ToolDispatcher,
    resolver: CallerScopeResolverPort,
    *,
    token: str = _TOKEN,
) -> AsyncIterator[ClientSession]:
    """Real transport end to end over an ALREADY-built registry/dispatcher:
    three permission-scoped `MCPServer`s (`build_mcp_servers`) ->
    `SeatCredentialRouter` (`build_mcp_asgi_apps`) -> the MCP SDK's
    `ClientSession` over `httpx.ASGITransport`. `mcp_session` below is the
    common case (the app factory's own registry); `test_journey_accounts.
    py` calls this directly with a hand-built registry to substitute the
    `OAuthBrokerPort` seam without a real broker socket."""
    mcp_servers = build_mcp_servers(
        registries=registries_by_permission(registry), dispatcher=dispatcher
    )
    mcp_router, mcp_apps = build_mcp_asgi_apps(
        mcp_servers, caller_scope_resolver=resolver, public_base_url=BASE_URL
    )
    async with AsyncExitStack() as stack:
        for mcp_app in mcp_apps.values():
            await stack.enter_async_context(mcp_app.router.lifespan_context(mcp_app))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mcp_router),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            async with streamable_http_client(f"{BASE_URL}/mcp", http_client=client) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session


def mcp_session(
    container: Container, resolver: CallerScopeResolverPort, *, token: str = _TOKEN
) -> AbstractAsyncContextManager[ClientSession]:
    """Real transport over `_build_mcp_registry_and_dispatcher` (the exact
    function `create_app` calls). `creative_generation=AsyncMock()`: no
    journey here calls a `generate_*` tool (same substitution `tests/
    integration/composition/test_campaign_drafts.py` uses)."""
    registry, dispatcher = _build_mcp_registry_and_dispatcher(
        container, container.settings, AsyncMock()
    )
    return mcp_session_over_registry(registry, dispatcher, resolver, token=token)


def unwrap(reply: CallToolResult) -> tuple[dict | None, dict | None]:
    """`mount.py`'s wrapper never raises a protocol-level error for a typed
    `ToolDispatchError`: it returns `{"error": {...}}` as an ordinary
    (`is_error=False`) result (verified against the real transport, not
    assumed). Returns `(result, error)`, exactly one non-`None`."""
    payload = reply.structured_content or {}
    return payload.get("result"), payload.get("error")
