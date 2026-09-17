"""Real MCP dispatcher -> PG proposal -> human signature -> queue/socket/receipt.

Only the provider SDK transport is a fake. Separate migrated database per test.
"""

import asyncio
import json
import os
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError
from sqlalchemy import text

from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.broker.infrastructure.adapter_registry import PlatformAdapterRegistry
from safent_ads.broker.infrastructure.caps_config import parse_caps_config
from safent_ads.broker.infrastructure.write_ledger_store import WriteLedgerStore
from safent_ads.broker.platforms.google_ads_adapter import GoogleAdsAdapter, GoogleAdsAdapterConfig
from safent_ads.broker.platforms.meta_ads_adapter import MetaAdsAdapter, MetaAdsAdapterConfig
from safent_ads.broker.platforms.write_pipeline import WriteAuthorizationPipeline
from safent_ads.broker.presentation.socket_server import serve
from safent_ads.composition.container import Container
from safent_ads.composition.managed_service import ManagedAdsService
from safent_ads.composition.mcp_write_adapter import ContainerProposalWriteAdapter
from safent_ads.execution.domain.execution_attempt import ExecutionStatus
from safent_ads.iam.application.managed_ads_authority import ManagedAdsAdmission, ManagedAdsDenied
from safent_ads.mcp.application.errors import ToolValidationError
from safent_ads.mcp.infrastructure.rate_limiter import InMemoryQuota
from safent_ads.mcp.presentation.catalog import build_default_registry, registries_by_permission
from safent_ads.mcp.presentation.dispatcher import ToolDispatcher
from safent_ads.mcp.presentation.http import build_mcp_asgi_apps, build_mcp_servers
from safent_ads.proposals.application.submit_approval import SubmitApprovalCommand
from safent_ads.proposals.domain.authorization import AuthorizationChannel
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.presentation.panel_read import proposal_detail
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.crypto.ed25519 import ApprovalVerifier
from safent_ads.shared.ids import EntityRef, PlatformCode
from safent_ads.shared.managed_ads import ManagedAdsBinding
from tests.conftest import OwnerFactory
from tests.contracts.execution.conftest import NOW, seed_freshness
from tests.integration.composition.test_write_path_end_to_end import (
    _APPROVAL_PUBLIC_KEY_B64,
    _APPROVAL_SEED_B64,
    _broker_runtime,
    _caps_yaml,
)
from tests.integration.execution.test_chokepoint_budget_race_regression import _run
from tests.integration.execution.test_chokepoint_budget_race_regression import (
    isolated_database_url as isolated_database_url,  # noqa: PLC0414
)
from tests.integration.mcp.test_campaign_creation_plan import _TestScopeResolver
from tests.integration.mcp.test_opportunity_tools import _caller
from tests.integration.mcp.test_write_tools import _minimal_read_model_ports
from tests.integration.opportunities.test_sql_repositories import _seed_business_with_account
from tests.unit.broker.ledger_scope_fakes import fake_scope
from tests.unit.broker.platforms.test_ad_child_creation import child_plan
from tests.unit.broker.platforms.test_meta_inline_creative import inline_plan
from tests.unit.composition.factories import build_api_settings

pytestmark = pytest.mark.integration


@asynccontextmanager
async def child_broker(tmp_path, native, platform):
    pipeline = WriteAuthorizationPipeline(
        ApprovalVerifier.from_public_key_b64(_APPROVAL_PUBLIC_KEY_B64),
        parse_caps_config(_caps_yaml("123" if platform == "google" else "act_123")),
        WriteLedgerStore(tmp_path / "ledger.sqlite"),
        scope_resolver=fake_scope,
        clock=FixedClock(NOW),
    )
    adapter = (
        GoogleAdsAdapter(
            GoogleAdsAdapterConfig("test", "test", "test", "123"),
            native,
            FixedClock(NOW),
            write_pipeline=pipeline,
        )
        if platform == "google"
        else MetaAdsAdapter(
            MetaAdsAdapterConfig("test", "test", "test"),
            native,
            FixedClock(NOW),
            write_pipeline=pipeline,
        )
    )
    runtime = _broker_runtime(
        PlatformAdapterRegistry({PlatformCode(platform): adapter}), tmp_path / "credentials"
    )
    path = tmp_path / "broker.sock"
    server = await serve(path, runtime, frozenset({os.getuid()}))
    try:
        yield path
    finally:
        server.close()
        await server.wait_closed()


async def seed_parents(container, kind, row, platform="google"):
    refs = []
    account = "123" if platform == "google" else "act_123"
    campaign = (
        "customers/123/campaigns/456"
        if platform == "google"
        else ("act_123/456" if kind == "ad_set" else "act_123/333")
    )
    group = "customers/123/adGroups/456" if platform == "google" else "act_123/456"
    async with container.session_factory() as session:
        business = await _seed_business_with_account(session, suffix=uuid4().hex)
        owner = await OwnerFactory(session).create()
        for _ in range(2):
            connection, account_id, campaign_id = uuid4(), uuid4(), uuid4()
            await session.execute(
                text("""INSERT INTO platform_connections(id,business_id,owner_id,platform)
                VALUES(:id,:business,:owner,:platform)"""),
                {
                    "id": connection,
                    "business": business.value,
                    "owner": owner,
                    "platform": platform,
                },
            )
            await session.execute(
                text("""INSERT INTO platform_accounts
                (id,business_id,platform,connection_id,external_account_id,currency,timezone,api_tier,status)
                VALUES(:id,:business,:platform,:connection,:remote,'EUR','Europe/Madrid',:tier,'ACTIVE')"""),
                {
                    "id": account_id,
                    "business": business.value,
                    "connection": connection,
                    "platform": platform,
                    "remote": account,
                    "tier": "google_standard" if platform == "google" else "meta_full",
                },
            )
            await session.execute(
                text("""INSERT INTO ad_entities
                (id,business_id,platform_account_id,connection_id,platform,level,external_id,name,status,platform_state_hash)
                VALUES(:id,:business,:account,:connection,:platform,'campaign',:external,'Parent','PAUSED',:hash)"""),
                {
                    "id": campaign_id,
                    "business": business.value,
                    "account": account_id,
                    "connection": connection,
                    "platform": platform,
                    "external": campaign,
                    "hash": PlatformStateHash.compute(
                        {k: v for k, v in row.items() if not k.endswith(".resource_name")}
                    ).value,
                },
            )
            level, external = "campaign", campaign
            if kind == "ad":
                level, external = "ad_set", group
                await session.execute(
                    text("""INSERT INTO ad_entities
                    (business_id,platform_account_id,connection_id,platform,level,external_id,parent_id,parent_level,name,status,platform_state_hash)
                    VALUES(:business,:account,:connection,:platform,'ad_set',:external,:parent,'campaign','Group','PAUSED',:hash)"""),
                    {
                        "business": business.value,
                        "account": account_id,
                        "connection": connection,
                        "platform": platform,
                        "external": external,
                        "parent": campaign_id,
                        "hash": PlatformStateHash.compute(
                            {k: v for k, v in row.items() if not k.endswith(".resource_name")}
                        ).value,
                    },
                )
            ref = EntityRef.parse(f"{platform}:{level}:{business.value}:{connection}:{external}")
            refs.append(ref)
            await seed_freshness(session, ref, lag_minutes=1)
        await session.execute(
            text("""INSERT INTO guardrails
            (scope,business_id,currency,daily_cap_minor,monthly_cap_minor,budget_floor_minor,
             budget_ceiling_minor,max_step_pct,max_changes_per_entity_per_day)
            VALUES('business',:business,'EUR',3000,90000,0,3000,30,10)"""),
            {"business": business.value},
        )
        await session.commit()
    return business, refs


@pytest.mark.parametrize("platform", ["google", "meta"])
@pytest.mark.parametrize("kind", ["ad_set", "ad"])
async def test_managed_child_uses_admitted_binding_not_caller_scope(
    isolated_database_url, monkeypatch, platform, kind
):
    container = Container.build(build_api_settings(database_url=isolated_database_url))
    container.clock = FixedClock(NOW)
    try:
        business, refs = await seed_parents(container, kind, {"status": "PAUSED"}, platform)
        binding = ManagedAdsBinding(
            uuid4(),
            1,
            uuid4(),
            uuid4(),
            uuid4(),
            uuid4(),
            AccountRef(PlatformCode(platform), "123", business.value, refs[0].connection_id),
            1,
        )
        # Only the admission transport is substituted; real scoped writer and SQL persist.
        service = object.__new__(ManagedAdsService)
        service.container = container
        admission = AsyncMock(return_value=ManagedAdsAdmission(binding, 9999999999, "propose"))
        monkeypatch.setattr(service, "admit", admission)
        args = {
            "entity_ref": str(refs[0]),
            "child_plan": child_plan(platform, kind),
            "cause": {"text": "Propuesta administrada explícita"},
        }
        result = await service.call("synthetic", "propose_ad_child", args)
        admission.assert_awaited_once_with("synthetic", "propose")
        async with container.session_factory() as session:
            row = (
                (
                    await session.execute(
                        text("SELECT managed_binding,proposed_value FROM proposals WHERE id=:id"),
                        {"id": result.proposal_id},
                    )
                )
                .mappings()
                .one()
            )
            assert row["managed_binding"] == binding.as_claims()
            assert row["proposed_value"] == {
                "type": "json",
                "value": {"child_plan": args["child_plan"]},
            }
            assert (await session.execute(text("SELECT count(*) FROM approvals"))).scalar_one() == 0
        with pytest.raises(ValidationError):
            await service.call(
                "synthetic", "propose_ad_child", {**args, "business_id": str(business)}
            )
        with pytest.raises(ToolValidationError):
            await service.call(
                "synthetic", "propose_ad_child", {**args, "entity_ref": str(refs[1])}
            )
        wrong_account = replace(
            binding, account=replace(binding.account, external_account_id="999")
        )
        admission.return_value = ManagedAdsAdmission(wrong_account, 9999999999, "propose")
        with pytest.raises(ToolValidationError):
            await service.call("synthetic", "propose_ad_child", args)
        admission.side_effect = ManagedAdsDenied("revoked")
        with pytest.raises(ManagedAdsDenied):
            await service.call("synthetic", "propose_ad_child", args)
    finally:
        await container.aclose()


@pytest.mark.parametrize("inline", [False, True])
async def test_streamable_mcp_exposes_child_plan_schema_and_persists_exact_json(
    isolated_database_url,
    inline,
):
    container = Container.build(build_api_settings(database_url=isolated_database_url))
    container.clock = FixedClock(NOW)
    try:
        business, refs = await seed_parents(
            container, "ad", {"status": "PAUSED"}, "meta" if inline else "google"
        )
        registry = build_default_registry(
            _minimal_read_model_ports(),
            container.clock,
            write_port=ContainerProposalWriteAdapter(container),
        )
        dispatcher = ToolDispatcher(registry=registry, quota=InMemoryQuota(clock=container.clock))
        mcp_servers = build_mcp_servers(
            registries=registries_by_permission(registry), dispatcher=dispatcher
        )
        mcp_router, mcp_apps = build_mcp_asgi_apps(
            mcp_servers,
            caller_scope_resolver=_TestScopeResolver(_caller(business)),
            public_base_url="https://ads.test",
        )
        plan = inline_plan() if inline else child_plan(kind="ad")
        args = {
            "business_id": str(business),
            "entity_ref": str(refs[0]),
            "child_plan": plan,
            "cause": {"text": "Anuncio explícito"},
        }
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
                        tool = next(tool for tool in tools.tools if tool.name == "propose_ad_child")
                        assert "dsa_beneficiary" in str(tool.model_dump(by_alias=True))
                        assert "INHERIT_CAMPAIGN" in str(tool.model_dump(by_alias=True))
                        assert "creative_inline" in str(tool.model_dump(by_alias=True))
                        reply = await session.call_tool("propose_ad_child", {"args": args})
                        result = reply.model_dump(by_alias=True)["structuredContent"]["result"]
        async with container.session_factory() as session:
            detail = await proposal_detail(session, str(business), result["proposal_id"])
            assert json.loads(detail["diff"]["valor_propuesto"])["child_plan"] == plan
            assert detail["requires_expansion"] is True
            assert (await session.execute(text("SELECT count(*) FROM approvals"))).scalar_one() == 0
    finally:
        await container.aclose()


@pytest.mark.parametrize("kind", ["ad_set", "ad"])
@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("platform", ["google", "meta", "meta_inline"])
async def test_child_real_mcp_proposal_approval_queue_broker_and_unknown_restart(  # noqa: PLR0915 - signed path and durable replay matrix
    isolated_database_url, tmp_path, kind, unknown, platform
):
    inline = platform == "meta_inline"
    if inline:
        if kind != "ad":
            pytest.skip("Inline creative is only an ad plan.")
        platform = "meta"
    resource = "campaign" if kind == "ad_set" else "ad_group"
    row = {
        f"{resource}.resource_name": "customers/123/campaigns/456"
        if kind == "ad_set"
        else "customers/123/adGroups/456",
        f"{resource}.status": "PAUSED",
        "customer.currency_code": "EUR",
    }
    if platform == "meta":
        row = {"id": "456", "account_id": "123", "status": "PAUSED"}

    class Native:
        calls = 0

        def search_stream(self, *_):
            return iter([row])

        def get_node(self, *_):
            return row

        def prepare_child(self, parent, plan):
            assert plan["kind"] == kind and parent == (
                row[f"{resource}.resource_name"] if platform == "google" else "act_123/456"
            )

        def create_paused_child(self, parent, plan):
            self.calls += 1
            assert parent
            assert plan["status"] == "PAUSED"
            if unknown:
                raise TimeoutError("synthetic lost acknowledgement")
            return {
                "child_resource": (
                    "customers/123/adGroups/789"
                    if kind == "ad_set"
                    else "customers/123/adGroupAds/456~789"
                )
                if platform == "google"
                else "act_123/789",
                "status": "PAUSED",
            }

    native = Native()
    async with child_broker(tmp_path, native, platform) as socket:
        settings = build_api_settings(
            database_url=isolated_database_url,
            broker_socket_path=str(socket),
            approval_signing_key=_APPROVAL_SEED_B64,
        )
        container = Container.build(settings)
        container.clock = FixedClock(NOW)
        try:
            business, refs = await seed_parents(container, kind, row, platform)
            registry = build_default_registry(
                _minimal_read_model_ports(),
                container.clock,
                write_port=ContainerProposalWriteAdapter(container),
            )
            dispatcher = ToolDispatcher(
                registry=registry, quota=InMemoryQuota(clock=container.clock)
            )
            args = {
                "business_id": str(business),
                "entity_ref": str(refs[0]),
                "child_plan": inline_plan() if inline else child_plan(platform, kind),
                "cause": {"text": "Jerarquía explícita revisada"},
            }
            result = (
                await dispatcher.dispatch(
                    "propose_ad_child", args, caller_scope=_caller(business)
                )
            )["result"]
            proposal_id = ProposalId.parse(result["proposal_id"])
            assert result["classification"] == "important"
            async with container.session_factory() as session:
                detail = await proposal_detail(session, str(business), str(proposal_id))
                assert (
                    json.loads(detail["diff"]["valor_propuesto"])["child_plan"]
                    == args["child_plan"]
                )
                assert detail["diff"]["diff_hash"] == result["diff_hash"]
                cases = container.build_execution_use_cases(session)
                await cases.submit_approval.execute(
                    SubmitApprovalCommand(
                        proposal_id=proposal_id,
                        diff_hash=result["diff_hash"],
                        approved_by="test-owner",
                        channel=AuthorizationChannel.PANEL,
                    )
                )
                await session.commit()
            # Same physical parent through a second OAuth cannot create a duplicate
            # or reparent the already signed proposal.
            args["entity_ref"] = str(refs[1])
            repeated = (
                await dispatcher.dispatch(
                    "propose_ad_child", args, caller_scope=_caller(business)
                )
            )["result"]
            assert repeated["proposal_id"] == result["proposal_id"]
            assert repeated["diff_hash"] == result["diff_hash"]
            container.clock = FixedClock(NOW + timedelta(minutes=1))
            expected = ExecutionStatus.UNKNOWN if unknown else ExecutionStatus.EXECUTED
            assert await _run(container, proposal_id) == expected
            assert native.calls == 1
        finally:
            await container.aclose()
        restarted = Container.build(settings)
        restarted.clock = FixedClock(NOW + timedelta(minutes=16))
        try:
            assert await _run(restarted, proposal_id) == (
                ExecutionStatus.UNKNOWN if unknown else None
            )
            assert native.calls == 1
            async with restarted.session_factory() as session:
                reserved = (
                    await session.execute(
                        text("SELECT count(*) FROM execution_reservations WHERE state='ACTIVE'")
                    )
                ).scalar_one()
                assert reserved == int(unknown)
                assert (
                    await session.execute(text("SELECT count(*) FROM approvals"))
                ).scalar_one() == 1
        finally:
            await restarted.aclose()


@pytest.mark.parametrize("platform", ["google", "meta"])
async def test_concurrent_proposals_share_physical_parent_and_never_fallback_to_other_connection(
    isolated_database_url, platform
):
    container = Container.build(build_api_settings(database_url=isolated_database_url))
    container.clock = FixedClock(NOW)
    try:
        business, refs = await seed_parents(container, "ad_set", {"status": "PAUSED"}, platform)
        writer = ContainerProposalWriteAdapter(container)

        async def propose(ref, business_id=str(business)):
            return await writer.propose_ad_child(
                business_id=business_id,
                entity_ref=str(ref),
                child_plan=child_plan(platform),
                cause_text="Solicitud explícita",
            )

        results = await asyncio.gather(*(propose(ref) for ref in refs), return_exceptions=True)
        assert any(not isinstance(result, Exception) for result in results)
        async with container.session_factory() as session:
            assert (await session.execute(text("SELECT count(*) FROM proposals"))).scalar_one() == 1
            await session.execute(
                text(
                    "UPDATE platform_accounts SET status='SUSPENDED' "
                    "WHERE connection_id=:connection"
                ),
                {"connection": refs[0].connection_id},
            )
            await session.commit()
        # Other OAuth is still ACTIVE, but cannot substitute for this request.
        with pytest.raises(ToolValidationError):
            await propose(refs[0])
        with pytest.raises(ToolValidationError):
            await propose(refs[1], str(uuid4()))
        async with container.session_factory() as session:
            assert (await session.execute(text("SELECT count(*) FROM proposals"))).scalar_one() == 1
            assert (await session.execute(text("SELECT count(*) FROM approvals"))).scalar_one() == 0
    finally:
        await container.aclose()
