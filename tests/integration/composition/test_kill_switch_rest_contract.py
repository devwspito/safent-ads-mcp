"""Owner/CSRF/tenant and actual SQL safety state through both panel routes."""

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
from safent_ads.composition.execution_rest import build_execution_router
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import AuthenticatedCaller, get_authenticated_caller

pytestmark = pytest.mark.integration


def app_for(container, restricted=None):
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_middleware(CsrfMiddleware)
    app.include_router(build_execution_router(container))
    if restricted is not None:

        async def caller():
            return AuthenticatedCaller(frozenset({str(restricted)}))

        app.dependency_overrides[get_authenticated_caller] = caller
    return app


@pytest.fixture
async def accounts(container, two_businesses, authenticated_session):
    async with container.session_factory() as session:
        owner = (await session.execute(text("SELECT id FROM owners LIMIT 1"))).scalar_one()
        external = str(uuid4().int % 10**10)
        items = []
        for business, route in (
            (two_businesses.business_a, True),
            (two_businesses.business_a, True),
            (two_businesses.business_b, False),
        ):
            connection = None
            if route:
                connection = uuid4()
                await session.execute(
                    text(
                        "INSERT INTO platform_connections(id,business_id,owner_id,platform) "
                        "VALUES(:id,:business,:owner,'google')"
                    ),
                    {"id": connection, "business": business, "owner": owner},
                )
            row = (
                (
                    await session.execute(
                        text(
                            "INSERT INTO platform_accounts(business_id,platform,"
                            "external_account_id,"
                            "currency,timezone,api_tier,status,connection_id) "
                            "VALUES(:business,'google',:external,'EUR','Europe/Madrid',"
                            "'google_standard','ACTIVE',:connection) RETURNING id,account_ref"
                        ),
                        {"business": business, "external": external, "connection": connection},
                    )
                )
                .mappings()
                .one()
            )
            items.append(dict(row))
        await session.commit()
    return items


async def test_get_and_post_real_account_brake_preserve_siblings_and_remaining_business_brake(
    container,
    two_businesses,
    accounts,
    authenticated_session,
):
    params = {"business_id": str(two_businesses.business_a)}
    cookies = authenticated_session.cookies | {"ads_csrf": "synthetic"}
    headers = {"X-CSRF-Token": "synthetic"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(container)),
        base_url="http://test",
        cookies=cookies,
    ) as api:
        before = await api.get("/api/v1/kill-switch", params=params)
        assert before.status_code == 200, before.text
        assert before.json()["items"] == []
        assert len(before.json()["by_account"]) == 2
        account_body = {
            "scope_kind": "platform_account",
            "scope_id": accounts[0]["account_ref"],
            "mode": "ALL",
            "engaged": True,
            "reason": "Synthetic pause",
        }
        after = await api.post(
            "/api/v1/kill-switch", params=params, headers=headers, json=account_body
        )
        assert after.status_code == 200, after.text
        assert all(row["engaged"] for row in after.json()["by_account"])
        assert after.json()["items"][0]["engaged_by"].startswith("owner:")
        assert after.json() == (await api.get("/api/v1/kill-switch", params=params)).json()
        business = await api.post(
            "/api/v1/kill-switch",
            params=params,
            headers=headers,
            json={
                "scope_kind": "business",
                "scope_id": params["business_id"],
                "mode": "AUTONOMOUS",
                "engaged": True,
                "reason": "Synthetic autonomy pause",
            },
        )
        assert business.status_code == 200, business.text
        assert len(business.json()["items"]) == 2
        assert business.json()["effective"]["mode"] == "ALL"
        released = await api.post(
            "/api/v1/kill-switch",
            params=params,
            headers=headers,
            json={**account_body, "scope_id": accounts[1]["account_ref"], "engaged": False},
        )
        assert released.status_code == 200, released.text
        assert len(released.json()["items"]) == 1
        assert released.json()["effective"]["mode"] == "AUTONOMOUS"
        assert all(row["mode"] == "AUTONOMOUS" for row in released.json()["by_account"])
        other = await api.get(
            "/api/v1/kill-switch", params={"business_id": str(two_businesses.business_b)}
        )
        assert other.json()["items"] == []
        assert not other.json()["effective"]["engaged"]


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("anonymous", 401),
        ("bearer", 401),
        ("csrf", 403),
        ("restricted", 404),
        ("cross_account", 404),
        ("cross_business", 404),
        ("scoped_global", 404),
        ("missing", 404),
        ("invalid_bool", 422),
    ],
)
async def test_write_boundary_never_mutates_on_denial(
    container,
    two_businesses,
    accounts,
    authenticated_session,
    *,
    mode,
    expected,
):
    restricted = (
        two_businesses.business_b
        if mode == "restricted"
        else (two_businesses.business_a if mode == "scoped_global" else None)
    )
    cookies = authenticated_session.cookies if mode not in {"anonymous", "bearer"} else {}
    cookies = cookies | {"ads_csrf": "synthetic"}
    body = {
        "scope_kind": "platform_account",
        "scope_id": accounts[0]["account_ref"],
        "mode": "ALL",
        "engaged": True,
        "reason": "Synthetic boundary test",
    }
    if mode == "cross_account":
        body["scope_id"] = accounts[2]["account_ref"]
    if mode == "cross_business":
        body.update(scope_kind="business", scope_id=str(two_businesses.business_b))
    if mode == "scoped_global":
        body.update(scope_kind="global", scope_id=None)
    if mode == "invalid_bool":
        body["engaged"] = "false"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(container, restricted)),
        base_url="http://test",
        cookies=cookies,
    ) as api:
        response = await api.post(
            "/api/v1/kill-switch",
            params={
                "business_id": str(uuid4() if mode == "missing" else two_businesses.business_a)
            },
            headers={}
            if mode == "csrf"
            else {"X-CSRF-Token": "synthetic", "Authorization": "Bearer synthetic"},
            json=body,
        )
    assert response.status_code == expected, response.text
    async with container.session_factory() as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM emergency_brakes WHERE business_id=:b "
                    "OR platform_account_id IN "
                    "(SELECT id FROM platform_accounts WHERE business_id=:b)"
                ),
                {"b": two_businesses.business_a},
            )
        ).scalar_one()
        assert count == 0


async def test_global_is_included_and_all_beats_global_autonomous(
    container,
    two_businesses,
    accounts,
    authenticated_session,
):
    # Seed only synthetic DB rows; no platform adapter or external execution.
    async with container.session_factory() as session:
        global_id = (
            await session.execute(
                text(
                    "INSERT INTO emergency_brakes(scope_kind,mode,reason,engaged_by) "
                    "VALUES('global','AUTONOMOUS','Synthetic global','fixture') RETURNING id"
                )
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO emergency_brakes(scope_kind,business_id,mode,reason,engaged_by) "
                "VALUES('business',:b,'ALL','Synthetic specific','fixture')"
            ),
            {"b": two_businesses.business_a},
        )
        await session.commit()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_for(container)),
            base_url="http://test",
            cookies=authenticated_session.cookies,
        ) as api:
            response = await api.get(
                "/api/v1/kill-switch", params={"business_id": str(two_businesses.business_a)}
            )
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 2
        assert response.json()["effective"]["mode"] == "ALL"
        assert response.json()["effective"]["scope_kind"] == "business"
        assert any(
            row["scope_kind"] == "global" and row["scope_id"] is None
            for row in response.json()["items"]
        )
    finally:
        async with container.session_factory() as session:
            await session.execute(
                text(
                    "UPDATE emergency_brakes SET released_at=now(), released_by='fixture' "
                    "WHERE id=:id"
                ),
                {"id": global_id},
            )
            await session.commit()


async def test_owner_global_toggle_returns_same_contract_and_released_rows_are_not_active(
    container,
    two_businesses,
    accounts,
    authenticated_session,
):
    params = {"business_id": str(two_businesses.business_a)}
    body = {
        "scope_kind": "global",
        "scope_id": None,
        "mode": "ALL",
        "engaged": True,
        "reason": "Synthetic global toggle",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(container)),
        base_url="http://test",
        cookies=authenticated_session.cookies | {"ads_csrf": "synthetic"},
    ) as api:
        engaged = await api.post(
            "/api/v1/kill-switch", params=params, headers={"X-CSRF-Token": "synthetic"}, json=body
        )
        assert engaged.status_code == 200, engaged.text
        assert engaged.json()["effective"]["scope_kind"] == "global"
        assert engaged.json()["effective"]["scope_id"] is None
        assert all(row["mode"] == "ALL" for row in engaged.json()["by_account"])
        released = await api.post(
            "/api/v1/kill-switch",
            params=params,
            headers={"X-CSRF-Token": "synthetic"},
            json={**body, "engaged": False},
        )
        assert released.status_code == 200, released.text
        assert released.json()["items"] == []
        assert not released.json()["effective"]["engaged"]
        assert released.json() == (await api.get("/api/v1/kill-switch", params=params)).json()


@pytest.mark.parametrize(
    "mode,expected", [("anonymous", 401), ("restricted", 404), ("missing", 404)]
)
async def test_get_authentication_and_business_existence_are_enforced(
    container,
    two_businesses,
    authenticated_session,
    *,
    mode,
    expected,
):
    app = app_for(container, two_businesses.business_b if mode == "restricted" else None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={} if mode == "anonymous" else authenticated_session.cookies,
    ) as api:
        result = await api.get(
            "/api/v1/kill-switch",
            params={
                "business_id": str(uuid4() if mode == "missing" else two_businesses.business_a)
            },
        )
    assert result.status_code == expected, result.text
