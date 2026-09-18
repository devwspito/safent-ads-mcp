"""Journey 1: what a `ver`/`proponer` person -- and single-owner mode
(Hermes) -- can reach through the real MCP HTTP transport (contracts/
mcp.md §3-4). Regression net for the harness (Claude Code/Codex over MCP)
hitting companion 0.2.19's permission surface, ahead of hotfix/0.2.20.

`test_ver_cannot_call_propose_campaign_draft` pins bug 1 (now fixed):
`catalog.py::registries_by_permission` still filters `tools/list` by
`tool_class`, but `mount.py::mount_tools` now gates `tools/call` for a
name that exists in the full catalog yet is not mounted for this
permission through `ToolDispatcher.deny` -- a `decision_log` row is
written (`outcome="denied"`, `error_code="PERMISSION_DENIED"`), matching
`dispatcher.py`'s own promise: "exito o fallo, SIEMPRE una fila"."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from safent_ads.composition.container import Container
from safent_ads.mcp.application.caller_scope import Permission
from safent_ads.mcp.infrastructure.single_owner_caller_scope_resolver import (
    SingleOwnerCallerScopeResolver,
)
from tests.e2e.journeys.conftest import (
    APPROVE_TOOL_COUNT,
    PROPOSE_TOOL_COUNT,
    READ_TOOL_COUNT,
    _FixedScopeResolver,
    mcp_session,
    person_scope,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def business_id(container: Container) -> uuid.UUID:
    business_id = uuid.uuid4()
    async with container.session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
                "VALUES (:id, :slug, 'Negocio de permisos', 'Europe/Madrid', 'EUR')"
            ),
            {"id": business_id, "slug": f"perm-{business_id.hex[:10]}"},
        )
        await session.commit()
    return business_id


async def test_ver_lists_only_the_pinned_read_catalog(
    container: Container, business_id: uuid.UUID
) -> None:
    resolver = _FixedScopeResolver(person_scope(Permission.VIEW, business_id=business_id))
    async with mcp_session(container, resolver) as session:
        tools = await session.list_tools()

    names = {tool.name for tool in tools.tools}
    assert len(names) == READ_TOOL_COUNT
    assert {"get_store_api_status", "get_store_catalog"} <= names
    assert not any(name.startswith(("propose_", "connect_platform_account")) for name in names)


async def test_ver_cannot_call_propose_campaign_draft(
    container: Container, business_id: uuid.UUID
) -> None:
    resolver = _FixedScopeResolver(person_scope(Permission.VIEW, business_id=business_id))
    async with mcp_session(container, resolver) as session:
        reply = await session.call_tool(
            "propose_campaign_draft",
            {"args": {"business_id": str(business_id), "draft_key": "x", "changes": {}}},
        )

    # `mount.py`'s wrapper never raises a protocol-level error for a denial:
    # it returns `{"error": {...}}` as an ordinary (`is_error=False`) result,
    # same convention as any other `ToolDispatchError` (`conftest.unwrap`).
    assert reply.is_error is False
    assert reply.structured_content["error"]["code"] == "PERMISSION_DENIED"
    async with container.session_factory() as db_session:
        raw_payload = (
            await db_session.execute(
                text("SELECT payload FROM decision_log WHERE business_id = :b"),
                {"b": business_id},
            )
        ).scalar_one()
    payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    assert payload["outcome"] == "denied"
    assert payload["error_code"] == "PERMISSION_DENIED"
    assert payload["tool"] == "propose_campaign_draft"


async def test_proponer_reaches_the_proposal_tool_class(
    container: Container, business_id: uuid.UUID
) -> None:
    resolver = _FixedScopeResolver(person_scope(Permission.PROPOSE, business_id=business_id))
    async with mcp_session(container, resolver) as session:
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert len(names) == PROPOSE_TOOL_COUNT
        assert "propose_campaign_draft" in names

        reply = await session.call_tool(
            "list_campaign_drafts", {"args": {"business_id": str(business_id)}}
        )
    assert reply.is_error is False
    assert reply.structured_content["result"]["items"] == []


async def test_single_owner_mode_serves_the_full_approve_catalog(
    container: Container, business_id: uuid.UUID
) -> None:
    """Hermes (`ADS_SINGLE_OWNER_MODE=true`): the real `SingleOwnerCallerScopeResolver`
    over `container.session_factory`, not a fake -- it is the piece under
    test (queries active businesses instead of trusting a static scope)."""
    resolver = SingleOwnerCallerScopeResolver(
        container.session_factory, expected_token="owner-static-token"
    )
    async with mcp_session(container, resolver, token="owner-static-token") as session:
        tools = await session.list_tools()

    names = {tool.name for tool in tools.tools}
    assert len(names) == APPROVE_TOOL_COUNT
    assert "connect_platform_account" in names
