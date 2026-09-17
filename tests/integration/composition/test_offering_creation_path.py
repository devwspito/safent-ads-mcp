"""Authenticated owner/MCP → real PostgreSQL catalog → pending proposal, no provider."""

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from tests.integration.composition.test_offerings_rest import (
    _app,
)
from tests.integration.composition.test_offerings_rest import (
    container as container,  # noqa: PLC0414 - pytest fixture re-export
)
from tests.integration.composition.test_offerings_rest import (
    two_businesses as two_businesses,  # noqa: PLC0414 - pytest fixture re-export
)
from tests.integration.mcp.test_opportunity_tools import _campaign_args
from tests.unit.execution.test_campaign_creation_budget import creation_payload

from safent_ads.catalog.application.create_offering import CreateOffering, OfferingCodeConflictError
from safent_ads.catalog.domain.offering import OfferingDetails
from safent_ads.catalog.infrastructure.sql_offering_creation import RequestScopedOfferingCreation
from safent_ads.composition.api import CsrfMiddleware
from safent_ads.composition.app import _build_mcp_registry_and_dispatcher
from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.errors import BusinessForbiddenError
from safent_ads.panel.presentation.deps import AuthenticatedCaller, get_authenticated_caller
from safent_ads.shared.ids import BusinessId

pytestmark = pytest.mark.integration
_BODY = {
    "code": "new-product",
    "title": "Explicit owner product",
    "price_amount": None,
    "price_currency": None,
}


def _client(container, cookies, *, restricted=False):
    app = _app(container)
    app.add_middleware(CsrfMiddleware)
    if restricted:

        async def caller():
            return AuthenticatedCaller(frozenset())

        app.dependency_overrides[get_authenticated_caller] = caller
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", cookies=cookies
    )


async def test_owner_creates_replays_conflicts_and_lists_without_inferred_price(
    container,
    two_businesses,
    authenticated_session,
):
    cookies = authenticated_session.cookies | {"ads_csrf": "synthetic-csrf"}
    params = {"business_id": str(two_businesses.business_a)}
    async with _client(container, cookies) as client:
        first = await client.post(
            "/api/v1/offerings",
            params=params,
            json=_BODY,
            headers={"X-CSRF-Token": "synthetic-csrf"},
        )
        assert first.status_code == 200, first.text
        assert first.json()["created"] is True
        assert first.json()["price_amount"] is None and first.json()["price_currency"] is None
        replay = await client.post(
            "/api/v1/offerings",
            params=params,
            json=_BODY,
            headers={"X-CSRF-Token": "synthetic-csrf"},
        )
        assert replay.json()["offering_id"] == first.json()["offering_id"]
        assert replay.json()["created"] is False
        conflict = await client.post(
            "/api/v1/offerings",
            params=params,
            json=_BODY | {"title": "Changed"},
            headers={"X-CSRF-Token": "synthetic-csrf"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "OFFERING_CODE_CONFLICT"
        rows = (await client.get("/api/v1/offerings", params=params)).json()["items"]
        matches = [row for row in rows if row["code"] == _BODY["code"]]
        assert len(matches) == 1 and matches[0]["list_price"] is None


@pytest.mark.parametrize(
    "mode,status", [("anonymous", 401), ("bearer", 401), ("csrf", 403), ("cross_business", 404)]
)
async def test_owner_route_denies_unauthenticated_csrf_and_cross_business(
    container,
    two_businesses,
    authenticated_session,
    mode,
    status,
):
    cookies = {} if mode in {"anonymous", "bearer"} else authenticated_session.cookies
    headers = {"X-CSRF-Token": "synthetic-csrf"}
    if mode == "bearer":
        headers["Authorization"] = "Bearer synthetic-mcp-token"
    if mode == "csrf":
        headers = {}
    async with _client(
        container, cookies | {"ads_csrf": "synthetic-csrf"}, restricted=mode == "cross_business"
    ) as client:
        result = await client.post(
            "/api/v1/offerings",
            params={"business_id": str(two_businesses.business_a)},
            json=_BODY,
            headers=headers,
        )
        assert result.status_code == status
    async with container.session_factory() as session:
        assert (
            await session.execute(
                text("SELECT count(*) FROM offerings WHERE business_id=:business AND code=:code"),
                {"business": two_businesses.business_a, "code": _BODY["code"]},
            )
        ).scalar_one() == 0


async def test_parallel_duplicate_requests_are_idempotent_and_business_isolated(
    container, two_businesses
):
    service = CreateOffering(RequestScopedOfferingCreation(container.session_factory))
    details = OfferingDetails("race-product", "Race", "19.95", "EUR")
    business = BusinessId(two_businesses.business_a)
    results = await asyncio.gather(*(service.execute(business, details) for _ in range(4)))
    assert len({row.offering_id for row in results}) == 1
    assert sum(row.created for row in results) == 1
    other = await service.execute(BusinessId(two_businesses.business_b), details)
    assert other.offering_id != results[0].offering_id
    with pytest.raises(OfferingCodeConflictError):
        await service.execute(business, OfferingDetails("race-product", "Race", "20", "EUR"))


async def test_real_registered_mcp_creation_enables_pending_campaign_without_provider(
    container,
    two_businesses,
    authenticated_session,
):
    business = two_businesses.business_a
    async with container.session_factory() as session:
        connection = uuid4()
        await session.execute(
            text("""INSERT INTO platform_connections(id,business_id,owner_id,platform)
            VALUES(:id,:business,:owner,'google')"""),
            {"id": connection, "business": business, "owner": authenticated_session.owner_id},
        )
        account = (
            await session.execute(
                text("""INSERT INTO platform_accounts
            (business_id,platform,connection_id,external_account_id,currency,timezone,api_tier,status)
            VALUES(:business,'google',:connection,'1234567890','EUR','Europe/Madrid','google_standard','ACTIVE')
            RETURNING account_ref"""),
                {"business": business, "connection": connection},
            )
        ).scalar_one()
        before = (await session.execute(text("SELECT count(*) FROM executions"))).scalar_one()
        await session.commit()
    registry, dispatcher = _build_mcp_registry_and_dispatcher(
        container, container.settings, AsyncMock()
    )
    assert registry.get("create_offering") is not None
    caller = CallerScope(
        "owner-agent", frozenset({str(business)}), Permission.PROPOSE, "Agente de prueba"
    )
    with pytest.raises(BusinessForbiddenError):
        await dispatcher.dispatch(
            "create_offering",
            _BODY | {"business_id": str(two_businesses.business_b)},
            caller_scope=caller,
        )
    created = (
        await dispatcher.dispatch(
            "create_offering", _BODY | {"business_id": str(business)}, caller_scope=caller
        )
    )["result"]
    args = _campaign_args(str(business), created["offering_id"])
    args["account_ref"] = account
    args["creation_plan"] = creation_payload()["creation_plan"]
    proposed = (await dispatcher.dispatch("propose_campaign", args, caller_scope=caller))["result"]
    assert proposed["estado"] == "pending"
    async with container.session_factory() as session:
        assert (
            await session.execute(
                text("SELECT state FROM proposals WHERE id=:id"), {"id": proposed["proposal_id"]}
            )
        ).scalar_one() == "pending"
        assert (
            await session.execute(text("SELECT count(*) FROM executions"))
        ).scalar_one() == before
