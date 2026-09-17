"""New accounts stay visible with null policy; only an owner can configure them."""

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
from safent_ads.rules.presentation.rest import build_rules_router

pytestmark = pytest.mark.integration


def app_for(container):
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_middleware(CsrfMiddleware)
    app.include_router(build_rules_router(container))
    app.include_router(build_execution_router(container))
    return app


@pytest.fixture
async def accounts(container, two_businesses):
    async with container.session_factory() as session:
        items = []
        for business, platform, currency in (
            (two_businesses.business_a, "google", "EUR"),
            (two_businesses.business_a, "meta", "USD"),
            (two_businesses.business_a, "google", "EUR"),
            (two_businesses.business_b, "google", "GBP"),
        ):
            external = ("act_" if platform == "meta" else "") + f"{uuid4().int % 10**10:010d}"
            row = (
                (
                    await session.execute(
                        text(
                            "INSERT INTO platform_accounts(business_id,platform,"
                            "external_account_id,currency,timezone,api_tier,status) "
                            "VALUES(:business,:platform,:external,:currency,"
                            "'Europe/Madrid','google_standard','ACTIVE') "
                            "RETURNING id,account_ref"
                        ),
                        {
                            "business": business,
                            "platform": platform,
                            "external": external,
                            "currency": currency,
                        },
                    )
                )
                .mappings()
                .one()
            )
            items.append({**row, "external": external})
        for item, incomplete in ((items[1], False), (items[2], True)):
            await session.execute(
                text(
                    "INSERT INTO guardrails(scope,platform_account_id,currency,daily_cap_minor,"
                    "monthly_cap_minor,budget_floor_minor,budget_ceiling_minor,max_step_pct,"
                    "max_changes_per_entity_per_day) VALUES('platform_account',:id,:currency,"
                    ":daily,31000,0,1500,10,2)"
                ),
                {
                    "id": item["id"],
                    "currency": "EUR" if incomplete else "USD",
                    "daily": None if incomplete else 1000,
                },
            )
        await session.commit()
    return items


async def test_read_includes_missing_and_incomplete_without_defaults_or_other_business(
    container, two_businesses, accounts, authenticated_session
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(container)),
        base_url="http://test",
        cookies=authenticated_session.cookies,
    ) as api:
        result = await api.get(
            "/api/v1/guardrails/setup", params={"business_id": str(two_businesses.business_a)}
        )
    assert result.status_code == 200, result.text
    items = {row["account_ref"]: row for row in result.json()["items"]}
    assert len(items) == 3 and accounts[3]["account_ref"] not in items
    assert items[accounts[0]["account_ref"]] == {
        "account_ref": accounts[0]["account_ref"],
        "platform": "google",
        "platform_account_id": accounts[0]["external"],
        "display_name": None,
        "currency": "EUR",
        "guardrail": None,
    }
    meta = items[accounts[1]["account_ref"]]
    assert meta["platform_account_id"] == accounts[1]["external"]
    assert meta["platform_account_id"].startswith("act_") and meta["currency"] == "USD"
    assert meta["guardrail"]["daily_cap"] == 10 and meta["guardrail"]["currency"] == "USD"
    assert items[accounts[2]["account_ref"]]["guardrail"] is None
    async with container.session_factory() as session:
        assert (
            await session.execute(
                text("SELECT count(*) FROM decision_log WHERE business_id=:business"),
                {"business": two_businesses.business_a},
            )
        ).scalar_one() == 0


@pytest.mark.parametrize(
    "mode,status", [("anonymous", 401), ("bearer", 401), ("restricted", 404), ("missing", 404)]
)
async def test_setup_owner_and_business_boundary(
    container, two_businesses, accounts, authenticated_session, *, mode, status
):
    app = app_for(container)
    if mode == "restricted":

        async def restricted():
            return AuthenticatedCaller(frozenset({str(two_businesses.business_b)}))

        app.dependency_overrides[get_authenticated_caller] = restricted
    cookies = authenticated_session.cookies if mode in {"restricted", "missing"} else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", cookies=cookies
    ) as api:
        result = await api.get(
            "/api/v1/guardrails/setup",
            params={
                "business_id": str(uuid4() if mode == "missing" else two_businesses.business_a),
            },
            headers={"Authorization": "Bearer synthetic"} if mode == "bearer" else {},
        )
    assert result.status_code == status


@pytest.mark.parametrize("mode,status", [("owner", 200), ("csrf", 403), ("cross_account", 404)])
async def test_existing_put_creates_missing_policy_and_audits_without_enabling_auto(
    container, two_businesses, accounts, authenticated_session, *, mode, status
):
    account = accounts[3] if mode == "cross_account" else accounts[0]
    params = {"business_id": str(two_businesses.business_a)}
    cookies = authenticated_session.cookies | {"ads_csrf": "synthetic"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(container)),
        base_url="http://test",
        cookies=cookies,
    ) as api:
        result = await api.put(
            f"/api/v1/guardrails/{account['account_ref']}",
            params=params,
            json={
                "daily_cap": 10,
                "monthly_cap": 310,
                "budget_floor": 0,
                "budget_ceiling": 15,
                "max_step_pct": 10,
                "max_changes_per_entity_per_day": 2,
            },
            headers={} if mode == "csrf" else {"X-CSRF-Token": "synthetic"},
        )
        assert result.status_code == status, result.text
        refreshed = await api.get("/api/v1/guardrails/setup", params=params)
    google = next(
        row for row in refreshed.json()["items"] if row["account_ref"] == accounts[0]["account_ref"]
    )
    assert (google["guardrail"] is not None) is (mode == "owner")
    async with container.session_factory() as session:
        assert (
            await session.execute(
                text("SELECT count(*) FROM decision_log WHERE business_id=:business"),
                {"business": two_businesses.business_a},
            )
        ).scalar_one() == (1 if mode == "owner" else 0)
        assert (
            await session.execute(
                text(
                    "SELECT count(*) FROM platform_accounts WHERE autonomy_enabled "
                    "AND business_id=:business"
                ),
                {"business": two_businesses.business_a},
            )
        ).scalar_one() == 0
        assert (
            await session.execute(
                text("SELECT count(*) FROM proposals WHERE business_id=:business"),
                {"business": two_businesses.business_a},
            )
        ).scalar_one() == 0
