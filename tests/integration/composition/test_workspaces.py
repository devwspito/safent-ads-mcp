"""A workspace survives client handoff; commands preserve authorization boundaries."""

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from tests.integration.composition.test_offerings_rest import (
    container as container,  # noqa: PLC0414
)
from tests.integration.composition.test_offerings_rest import (
    two_businesses as two_businesses,  # noqa: PLC0414
)

from safent_ads.composition.api import CsrfMiddleware, _handle_api_error
from safent_ads.composition.app import _build_mcp_registry_and_dispatcher
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.errors import BusinessForbiddenError
from safent_ads.opportunities.domain.campaign_draft import DraftError, DraftFields
from safent_ads.opportunities.infrastructure.campaign_drafts_sql import CampaignDraftStore
from safent_ads.runtime.contracts import RuntimeResult
from safent_ads.runtime.store import RuntimeJobStore, enqueue_job
from safent_ads.workspaces.contracts import WorkspaceBrief
from safent_ads.workspaces.presentation import build_workspace_router
from safent_ads.workspaces.store import WorkspaceStore

pytestmark = pytest.mark.integration


async def test_runtime_complete_draft_advances_once_without_video_or_page(
    container, two_businesses, authenticated_session
):
    service = store(container)
    business = str(two_businesses.business_a)
    connection = uuid4()
    async with container.session_factory.begin() as session:
        await session.execute(
            text("""INSERT INTO platform_connections
            (id,business_id,owner_id,platform) VALUES(:id,:b,:owner,'meta')"""),
            {
                "id": connection,
                "b": two_businesses.business_a,
                "owner": authenticated_session.owner_id,
            },
        )
        account = (
            await session.execute(
                text("""INSERT INTO platform_accounts
            (business_id,platform,connection_id,external_account_id,currency,timezone,api_tier,status)
            VALUES(:b,'meta',:connection,:external,'EUR','Europe/Madrid','meta_limited','ACTIVE')
            RETURNING account_ref"""),
                {
                    "b": two_businesses.business_a,
                    "connection": connection,
                    "external": "act_" + str(uuid4().int)[:15],
                },
            )
        ).scalar_one()
        job = await enqueue_job(
            session,
            business,
            {
                "slug": "launch",
                "revision": "a" * 64,
                "title": "Launch",
                "blockers": [],
                "documents": [],
                "video_slots": [],
            },
        )
    runtime = RuntimeJobStore(container.session_factory, service.drafts)
    claim = (await runtime.claim(business, "codex"))["job"]
    assert claim["workspace"]["id"] == job["workspace_id"]
    report = RuntimeResult(
        outcome="blocked",
        summary="Video pending",
        blockers=["Video pending"],
        campaign=DraftFields(
            title="Launch",
            platform="meta",
            account_ref=account,
            offering_id=str(two_businesses.offering_a),
            objective="Leads",
            daily_budget={"amount": "15", "currency": "EUR"},
            duration_days=7,
            success_criterion="Customers",
            kill_criterion="Budget",
            angle="Invitation",
            targeting_seed="Local",
            landing_url="https://example.com/event",
        ),
    )
    first = await runtime.report(business, job["id"], "codex", claim["lease_token"], report)
    repeated = await runtime.report(business, job["id"], "codex", claim["lease_token"], report)
    assert first["state"] == "prepared", first
    assert first["result"]["proposal_id"] == repeated["result"]["proposal_id"]
    assert first["result"]["blockers"] == []
    assert "Video pending" in first["result"]["activation_blockers"]
    view = await service.get(business, job["workspace_id"])
    assert view["campaigns"][0]["step"]["state"] == "approval"
    assert view["campaigns"][0]["execution"] is None
    assert len(view["activity"]) == 1


def store(container):
    return WorkspaceStore(CampaignDraftStore(container.session_factory, container.clock))


def client(container, cookies):
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_middleware(CsrfMiddleware)
    app.include_router(build_workspace_router(store(container)))
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", cookies=cookies
    )


async def test_mcp_to_panel_to_other_runtime_same_context(
    container, two_businesses, authenticated_session
):
    business = str(two_businesses.business_a)
    _, dispatcher = _build_mcp_registry_and_dispatcher(container, container.settings, AsyncMock())
    caller = CallerScope("codex", frozenset({business}), Permission.PROPOSE, "test")
    result = (
        await dispatcher.dispatch(
            "propose_workspace",
            {
                "business_id": business,
                "workspace_key": "launch",
                "changes": {
                    "title": "Launch",
                    "objective": "Meet customers",
                    "schedule": "17 October",
                },
            },
            caller_scope=caller,
        )
    )["result"]
    cookies = authenticated_session.cookies | {"ads_csrf": "synthetic"}
    async with client(container, cookies) as api:
        read = await api.get(f"/api/v1/workspaces/{result['id']}", params={"business_id": business})
        assert read.status_code == 200, read.text
        assert read.json()["brief"]["schedule"] == "17 October"
        update = await api.post(
            "/api/v1/workspaces",
            params={"business_id": business},
            headers={"X-CSRF-Token": "synthetic"},
            json={
                "workspace_key": "launch",
                "expected_revision": 1,
                "changes": {"notes": "Confirmed by owner"},
            },
        )
        assert update.status_code == 200, update.text
    other = CallerScope("claude", frozenset({business}), Permission.PROPOSE, "test")
    reread = (
        await dispatcher.dispatch(
            "get_workspace",
            {"business_id": business, "workspace_id": result["id"]},
            caller_scope=other,
        )
    )["result"]
    assert reread["brief"]["objective"] == "Meet customers"
    assert reread["brief"]["notes"] == "Confirmed by owner"
    assert reread["revision"] == 2
    assert reread["activity"][0]["actor"].startswith("person:")
    assert reread["capabilities"]["automatic_activation"] is False


async def test_idempotency_revision_and_foreign_business(container, two_businesses):
    service = store(container)
    business = str(two_businesses.business_a)
    results = await asyncio.gather(
        *(
            service.save(business, "stable", None, WorkspaceBrief(title="Launch"), "codex")
            for _ in range(3)
        )
    )
    assert len({item["id"] for item in results}) == 1
    project = results[0]
    await service.save(business, "stable", 1, WorkspaceBrief(notes="new"), "claude")
    with pytest.raises(DraftError, match="CHANGED"):
        await service.save(business, "stable", 1, WorkspaceBrief(notes="old"), "codex")
    with pytest.raises(DraftError, match="NOT_FOUND"):
        await service.get(str(two_businesses.business_b), project["id"])
    _, dispatcher = _build_mcp_registry_and_dispatcher(container, container.settings, AsyncMock())
    with pytest.raises(BusinessForbiddenError):
        await dispatcher.dispatch(
            "get_workspace",
            {"business_id": business, "workspace_id": project["id"]},
            caller_scope=CallerScope(
                "foreign", frozenset({str(two_businesses.business_b)}), Permission.VIEW, "test"
            ),
        )


async def test_new_and_legacy_drafts_are_adopted_without_creation(container, two_businesses):
    service = store(container)
    business = str(two_businesses.business_a)
    project = await service.save(business, "launch", None, WorkspaceBrief(title="Launch"), "panel")
    draft = await service.save_campaign(
        business, project["id"], "campaign", None, DraftFields(title="Campaign"), "codex"
    )
    legacy = await service.drafts.save(business, "legacy", None, DraftFields(title="Legacy"))
    assert len((await service.list(business))["items"]) == 2
    detail = await service.get(business, project["id"])
    assert detail["campaigns"][0]["draft"]["draft_id"] == draft["draft_id"]
    assert detail["campaigns"][0]["step"]["state"] == "incomplete"
    assert detail["campaigns"][0]["proposal"] is None
    with pytest.raises(DraftError, match="NOT_FOUND"):
        await service.prepare(business, project["id"], legacy["draft_id"], 1, "codex")
    with pytest.raises(DraftError, match="INCOMPLETE"):
        await service.prepare(business, project["id"], draft["draft_id"], 1, "codex")


@pytest.mark.parametrize("mode,status", [("anonymous", 401), ("csrf", 403), ("owner", 200)])
async def test_workspace_writes_require_owner_and_csrf(
    container, two_businesses, authenticated_session, mode, status
):
    cookies = authenticated_session.cookies if mode != "anonymous" else {}
    async with client(container, cookies | {"ads_csrf": "synthetic"}) as api:
        response = await api.post(
            "/api/v1/workspaces",
            params={"business_id": str(two_businesses.business_a)},
            json={"workspace_key": "owner", "changes": {"title": "Owner"}},
            headers={} if mode == "csrf" else {"X-CSRF-Token": "synthetic"},
        )
        assert response.status_code == status, response.text
