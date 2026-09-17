"""Real MCP dispatcher -> scoped PostgreSQL proposal -> panel DTO -> approval."""

from contextlib import AsyncExitStack
from copy import deepcopy
from uuid import uuid4

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text
from tests.contracts.execution.conftest import NOW
from tests.integration.execution.test_campaign_creation_path import seed_creations
from tests.integration.execution.test_chokepoint_budget_race_regression import (
    isolated_database_url as isolated_database_url,  # noqa: PLC0414 - fixture re-export
)
from tests.integration.mcp.test_opportunity_tools import _build_dispatcher, _caller, _campaign_args
from tests.unit.composition.factories import build_api_settings
from tests.unit.execution.test_campaign_creation_budget import creation_payload

from safent_ads.composition.container import Container
from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.application.errors import ToolValidationError
from safent_ads.mcp.presentation.catalog import registries_by_permission
from safent_ads.mcp.presentation.http import build_mcp_asgi_apps, build_mcp_servers
from safent_ads.proposals.application.submit_approval import SubmitApprovalCommand
from safent_ads.proposals.domain.authorization import AuthorizationChannel
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.presentation.panel_read import proposal_detail
from safent_ads.shared.clock import FixedClock

pytestmark = pytest.mark.integration


class _TestScopeResolver:
    def __init__(self, scope: CallerScope) -> None:
        self._scope = scope

    async def resolve(self, token: str) -> CallerScope:
        assert token == "synthetic-plan-mcp-token"  # noqa: S105 - synthetic local MCP fixture
        return self._scope


async def _context(container):
    first, _ = await seed_creations(container)
    async with container.session_factory() as session:
        row = (
            await session.execute(
                text("SELECT business_id,entity_ref FROM proposals WHERE id=:id"),
                {"id": str(first)},
            )
        ).one()
        offering = uuid4()
        await session.execute(
            text("""INSERT INTO offerings
            (id,business_id,code,title,price_amount,price_currency)
            VALUES(:id,:business,'native-plan','Oferta explícita',1200,'EUR')"""),
            {"id": offering, "business": row.business_id},
        )
        await session.commit()
    args = _campaign_args(str(row.business_id), str(offering))
    args["account_ref"] = row.entity_ref
    return str(row.business_id), args


async def test_real_mcp_transport_publishes_schema_and_preserves_explicit_plan(
    isolated_database_url,
):
    container = Container.build(build_api_settings(database_url=isolated_database_url))
    container.clock = FixedClock(NOW)
    try:
        business, args = await _context(container)
        args["creation_plan"] = creation_payload()["creation_plan"]
        dispatcher = _build_dispatcher(container)
        mcp_servers = build_mcp_servers(
            registries=registries_by_permission(dispatcher._registry), dispatcher=dispatcher
        )
        mcp_router, mcp_apps = build_mcp_asgi_apps(
            mcp_servers,
            caller_scope_resolver=_TestScopeResolver(_caller(business)),
            public_base_url="https://ads.test",
        )
        async with AsyncExitStack() as stack:
            for mcp_app in mcp_apps.values():
                await stack.enter_async_context(mcp_app.router.lifespan_context(mcp_app))
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=mcp_router),
                headers={"Authorization": "Bearer synthetic-plan-mcp-token"},
            ) as client:
                async with streamable_http_client("https://ads.test/mcp", http_client=client) as (
                    read,
                    write,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        tool = next(tool for tool in tools.tools if tool.name == "propose_campaign")
                        assert "contains_eu_political_advertising" in str(tool.model_dump())
                        assert "special_ad_category_country" in str(tool.model_dump())
                        reply = await session.call_tool("propose_campaign", {"args": args})
                        result = reply.model_dump(by_alias=True)["structuredContent"]["result"]
        async with container.session_factory() as session:
            detail = await proposal_detail(session, business, result["proposal_id"])
            assert detail["creation_plan"] == args["creation_plan"]
            assert detail["diff"]["diff_hash"] == result["diff_hash"]
    finally:
        await container.aclose()


async def test_agent_explicit_plan_survives_regeneration_and_is_human_approvable(
    isolated_database_url,
):
    container = Container.build(build_api_settings(database_url=isolated_database_url))
    container.clock = FixedClock(NOW)
    try:
        business, args = await _context(container)
        plan = creation_payload()["creation_plan"]
        plan["name"] = "  Plan propuesto por agente  "
        plan["daily_budget"]["amount"] = "020.00"
        args["creation_plan"] = deepcopy(plan)
        dispatcher = _build_dispatcher(container)
        outcome = (
            await dispatcher.dispatch("propose_campaign", args, caller_scope=_caller(business))
        )["result"]
        proposal_id = outcome["proposal_id"]
        async with container.session_factory() as session:
            detail = await proposal_detail(session, business, proposal_id)
            assert detail["creation_plan"] == plan
            assert detail["creation_plan_error"] is None
            assert detail["diff"]["diff_hash"] == outcome["diff_hash"]
            assert detail["state"] == "pending"
        args.pop("creation_plan")
        repeated = (
            await dispatcher.dispatch("propose_campaign", args, caller_scope=_caller(business))
        )["result"]
        assert repeated["proposal_id"] == proposal_id
        assert repeated["diff_hash"] == outcome["diff_hash"]
        listed = (
            await dispatcher.dispatch(
                "list_opportunities", {"business_id": business}, caller_scope=_caller(business)
            )
        )["result"]
        assert listed[0]["brief"]["creation_plan"] == plan
        async with container.session_factory() as session:
            cases = container.build_execution_use_cases(session)
            await cases.submit_approval.execute(
                SubmitApprovalCommand(
                    proposal_id=ProposalId.parse(proposal_id),
                    diff_hash=outcome["diff_hash"],
                    approved_by="test-owner",
                    channel=AuthorizationChannel.PANEL,
                )
            )
            await session.commit()
            assert (
                await session.execute(
                    text(
                        "SELECT count(*) FROM approvals WHERE proposal_id=:id AND diff_hash=:hash"
                    ),
                    {"id": proposal_id, "hash": outcome["diff_hash"]},
                )
            ).scalar_one() == 1
            assert (
                await session.execute(text("SELECT count(*) FROM execution_reservations"))
            ).scalar_one() == 0  # Proposing/approving never calls a provider here.
    finally:
        await container.aclose()


@pytest.mark.parametrize("invalid", ["budget", "platform", "policy", "float", "version_bool"])
async def test_invalid_agent_plan_fails_before_any_proposal_is_persisted(
    isolated_database_url, invalid
):
    container = Container.build(build_api_settings(database_url=isolated_database_url))
    container.clock = FixedClock(NOW)
    try:
        business, args = await _context(container)
        plan = creation_payload()["creation_plan"]
        if invalid == "budget":
            plan["daily_budget"]["amount"] = "25.00"
        elif invalid == "platform":
            args["platform"] = "meta"
        elif invalid == "policy":
            del plan["native"]["contains_eu_political_advertising"]
        elif invalid == "float":
            plan["daily_budget"]["amount"] = 20.0
        else:
            plan["schema_version"] = True
        args["creation_plan"] = plan
        with pytest.raises(ToolValidationError):
            await _build_dispatcher(container).dispatch(
                "propose_campaign", args, caller_scope=_caller(business)
            )
        async with container.session_factory() as session:
            assert (await session.execute(text("SELECT count(*) FROM proposals"))).scalar_one() == 2
    finally:
        await container.aclose()
