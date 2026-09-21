"""Durable incomplete drafts and atomic promotion, with no provider or approval."""

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from tests.integration.composition.test_offerings_rest import (
    container as container,  # noqa: PLC0414 - pytest fixture re-export
)  # noqa: PLC0414
from tests.integration.composition.test_offerings_rest import (
    two_businesses as two_businesses,  # noqa: PLC0414 - pytest fixture re-export
)
from tests.unit.execution.test_campaign_creation_budget import creation_payload

from safent_ads.composition.api import CsrfMiddleware, _handle_api_error
from safent_ads.composition.app import _build_mcp_registry_and_dispatcher
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.errors import BusinessForbiddenError
from safent_ads.opportunities.domain.campaign_draft import DraftError, DraftFields
from safent_ads.opportunities.infrastructure.campaign_drafts_sql import CampaignDraftStore
from safent_ads.opportunities.presentation.campaign_drafts_rest import build_campaign_drafts_router
from safent_ads.workspaces.store import WorkspaceStore

pytestmark = pytest.mark.integration


# La base `ads_isolated` es de toda la sesion de pytest: un `count(*)` global
# cuenta tambien las filas que dejo otro test (e2e, mcp, execution...). Lo que
# este banco afirma es del NEGOCIO bajo prueba -- que redactar un borrador no
# crea propuesta, aprobacion ni ejecucion suya -- asi que cada conteo va
# acotado por `business_id`, como toda consulta de dominio.
_SCOPED_COUNTS = {
    "proposals": "SELECT count(*) FROM proposals WHERE business_id = :business",
    "approvals": (
        "SELECT count(*) FROM approvals a JOIN proposals p ON p.id = a.proposal_id "
        "WHERE p.business_id = :business"
    ),
    "executions": "SELECT count(*) FROM executions WHERE business_id = :business",
}


async def count_for_business(container, business, table, predicate=""):
    async with container.session_factory() as session:
        result = await session.execute(
            text(_SCOPED_COUNTS[table] + predicate), {"business": business}
        )
        return result.scalar_one()


def client(container, cookies):
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_middleware(CsrfMiddleware)
    app.include_router(
        build_campaign_drafts_router(CampaignDraftStore(container.session_factory, container.clock))
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", cookies=cookies
    )


async def test_incomplete_durable_edit_and_optimistic_revision(container, two_businesses):
    business = str(two_businesses.business_a)
    store = CampaignDraftStore(container.session_factory, container.clock)
    first = await store.save(
        business,
        "owner-idea",
        None,
        DraftFields(title="Owner idea", notes="Budget and URL tomorrow"),
    )
    assert first["state"] == "draft" and first["executable"] is False
    assert first["brief"]["daily_budget"] is None and first["brief"]["landing_url"] is None
    assert "daily_budget" in first["missing_fields"]
    assert (
        await store.save(
            business,
            "owner-idea",
            None,
            DraftFields(title="Owner idea", notes="Budget and URL tomorrow"),
        )
    )["draft_id"] == first["draft_id"]
    with pytest.raises(DraftError, match="INCOMPLETE"):
        await store.promote(business, first["draft_id"], 1)
    updated = await store.save(
        business, "owner-idea", 1, DraftFields(landing_url="https://example.com/reserve")
    )
    assert updated["revision"] == 2 and updated["brief"]["notes"] == first["brief"]["notes"]
    with pytest.raises(DraftError, match="CHANGED"):
        await store.save(business, "owner-idea", 1, DraftFields(notes="stale"))
    restarted = CampaignDraftStore(container.session_factory, container.clock)
    assert (await restarted.get(business, first["draft_id"]))["brief"][
        "landing_url"
    ] == "https://example.com/reserve"
    assert len((await restarted.list(business))["items"]) == 1
    for table in ("proposals", "approvals", "executions"):
        assert await count_for_business(container, two_businesses.business_a, table) == 0


@pytest.mark.parametrize("kind", ["foreign_draft", "foreign_offering", "foreign_mcp"])
async def test_cross_business_never_discloses_or_mutates(container, two_businesses, kind):
    business = str(two_businesses.business_a)
    other = str(two_businesses.business_b)
    store = CampaignDraftStore(container.session_factory, container.clock)
    first = await store.save(business, "private", None, DraftFields(title="Private"))
    if kind == "foreign_draft":
        for action in (
            store.get(other, first["draft_id"]),
            store.promote(other, first["draft_id"], 1),
        ):
            with pytest.raises(DraftError, match="NOT_FOUND"):
                await action
    elif kind == "foreign_offering":
        with pytest.raises(DraftError, match="NOT_FOUND"):
            await store.save(
                business, "private", 1, DraftFields(offering_id=str(two_businesses.offering_b))
            )
    else:
        _, dispatcher = _build_mcp_registry_and_dispatcher(
            container, container.settings, AsyncMock()
        )
        with pytest.raises(BusinessForbiddenError):
            await dispatcher.dispatch(
                "get_campaign_draft",
                {"business_id": business, "draft_id": first["draft_id"]},
                caller_scope=CallerScope(
                    "test", frozenset({other}), Permission.PROPOSE, "Agente de prueba"
                ),
            )
    assert (await store.get(business, first["draft_id"]))["revision"] == 1


@pytest.mark.parametrize(
    "mode,status", [("anonymous", 401), ("bearer", 401), ("csrf", 403), ("owner", 200)]
)
async def test_owner_route_requires_real_session_and_csrf(
    container, two_businesses, authenticated_session, mode, status
):
    cookies = authenticated_session.cookies if mode in {"owner", "csrf"} else {}
    headers = {} if mode == "csrf" else {"X-CSRF-Token": "synthetic"}
    if mode == "bearer":
        headers["Authorization"] = "Bearer synthetic"
    async with client(container, cookies | {"ads_csrf": "synthetic"}) as api:
        result = await api.post(
            "/api/v1/campaign-drafts",
            params={"business_id": str(two_businesses.business_a)},
            json={"draft_key": "idea", "changes": {"title": "Explicit idea"}},
            headers=headers,
        )
        assert result.status_code == status, result.text
        if mode == "owner":
            promoted = await api.post(
                f"/api/v1/campaign-drafts/{result.json()['draft_id']}/propose",
                params={"business_id": str(two_businesses.business_a)},
                json={"expected_revision": 1},
                headers=headers,
            )
            assert promoted.status_code == 422
            assert promoted.json()["error"]["code"] == "CAMPAIGN_DRAFT_INCOMPLETE"


async def test_complete_mcp_draft_promotes_atomically_once_and_never_approves(
    container, two_businesses, authenticated_session
):
    business = str(two_businesses.business_a)
    connection = uuid4()
    async with container.session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO platform_connections(id,business_id,owner_id,platform) "
                "VALUES(:id,:business,:owner,'google')"
            ),
            {
                "id": connection,
                "business": two_businesses.business_a,
                "owner": authenticated_session.owner_id,
            },
        )
        account = (
            await session.execute(
                text(
                    "INSERT INTO platform_accounts(business_id,platform,connection_id,"
                    "external_account_id,currency,timezone,api_tier,status) "
                    "VALUES(:business,'google',:connection,'1234567890','EUR','Europe/Madrid',"
                    "'google_standard','ACTIVE') RETURNING account_ref"
                ),
                {"business": two_businesses.business_a, "connection": connection},
            )
        ).scalar_one()
        await session.commit()
    _, dispatcher = _build_mcp_registry_and_dispatcher(container, container.settings, AsyncMock())
    caller = CallerScope(
        "owner-agent", frozenset({business}), Permission.PROPOSE, "Agente de prueba"
    )
    saved = (
        await dispatcher.dispatch(
            "propose_campaign_draft",
            {
                "business_id": business,
                "draft_key": "tomorrow",
                "changes": {
                    "title": "Tomorrow",
                    "platform": "google",
                    "offering_id": str(two_businesses.offering_a),
                    "account_ref": account,
                },
            },
            caller_scope=caller,
        )
    )["result"]
    assert saved["brief"]["daily_budget"] is None
    plan = creation_payload()["creation_plan"]
    changes = {
        "objective": "Owner objective",
        "daily_budget": plan["daily_budget"],
        "duration_days": 7,
        "success_criterion": "Owner success",
        "kill_criterion": "Owner stop",
        "angle": "Owner angle",
        "targeting_seed": "Owner audience",
        "landing_url": "https://example.com/reserve",
        "creation_plan": plan,
    }
    updated = (
        await dispatcher.dispatch(
            "propose_campaign_draft",
            {
                "business_id": business,
                "draft_key": "tomorrow",
                "expected_revision": 1,
                "changes": changes,
            },
            caller_scope=caller,
        )
    )["result"]
    assert not updated["missing_fields"]
    store = CampaignDraftStore(container.session_factory, container.clock)
    envelopes = await asyncio.gather(
        *(
            dispatcher.dispatch(
                "propose_campaign_from_draft",
                {
                    "business_id": business,
                    "draft_id": saved["draft_id"],
                    "expected_revision": 2,
                },
                caller_scope=caller,
            )
            for _ in range(3)
        )
    )
    results = [envelope["result"] for envelope in envelopes]
    assert len({item["proposal_id"] for item in results}) == 1
    assert all(item["state"] == "proposed" and not item["executable"] for item in results)
    workspace_store = WorkspaceStore(store)
    project = (await workspace_store.list(business))["items"][0]
    views = await asyncio.gather(
        *(
            workspace_store.prepare(business, project["id"], saved["draft_id"], 2, "other-runtime")
            for _ in range(3)
        )
    )
    assert all(view["campaigns"][0]["step"]["state"] == "approval" for view in views)
    assert all(
        view["campaigns"][0]["proposal"]["id"] == results[0]["proposal_id"] for view in views
    )
    with pytest.raises(DraftError, match="ALREADY_PROPOSED"):
        await store.save(business, "tomorrow", 3, DraftFields(notes="Changed after promotion"))
    scope = two_businesses.business_a
    assert await count_for_business(container, scope, "proposals", " AND state='pending'") == 1
    assert await count_for_business(container, scope, "approvals") == 0
    assert await count_for_business(container, scope, "executions") == 0
