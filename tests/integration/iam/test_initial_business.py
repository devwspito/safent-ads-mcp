"""First entry uses real SSO, real HTTP composition and an empty PostgreSQL DB."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from tests.integration.iam.test_exchange_route import (
    _companion_settings,
    _payload,
    _sign,
)
from tests.integration.iam.test_exchange_route import keypair as keypair  # noqa: PLC0414
from tests.integration.iam.test_exchange_route import tls_material as tls_material  # noqa: PLC0414
from tests.unit.composition.test_managed_app import settings as managed_settings

from safent_ads.accounts.application.create_initial_business import (
    CreateInitialBusiness,
    OwnerConfigurationAmbiguousError,
)
from safent_ads.accounts.infrastructure.sql_business_bootstrap import SqlInitialBusinessRepository
from safent_ads.composition.app import create_app

pytestmark = pytest.mark.integration
PATH = "/api/v1/onboarding/business"
BODY = {"name": "  Mi negocio  ", "timezone": "Europe/Madrid", "reference_currency": "EUR"}
CSRF = {"X-CSRF-Token": "synthetic-first-entry-csrf"}


@pytest.fixture
async def empty_db(isolated_iam_database_url: str) -> AsyncIterator[str]:
    engine = create_async_engine(isolated_iam_database_url)
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM sessions"))
        await connection.execute(text("DELETE FROM owners"))
        await connection.execute(text("DELETE FROM sso_assertions_seen"))
        await connection.execute(text("DELETE FROM businesses"))
    try:
        yield isolated_iam_database_url
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM sessions"))
            await connection.execute(text("DELETE FROM owners"))
            await connection.execute(text("DELETE FROM sso_assertions_seen"))
            await connection.execute(text("DELETE FROM businesses"))
        await engine.dispose()


@pytest.fixture
async def first_entry(empty_db, keypair, tls_material):
    private, public = keypair
    app = create_app(
        _companion_settings(
            database_url=empty_db,
            sso_public_key=public,
            tls_material=tls_material,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://test"
    ) as client:
        try:
            response = await client.post(
                "/api/v1/auth/exchange", json={"assertion": _sign(private, _payload())}
            )
            assert response.status_code == 200, response.text
            assert response.json()["business_id"] is None
            assert (await client.get("/api/v1/auth/me")).json()["businesses"] == []
            client.cookies.set("ads_csrf", CSRF["X-CSRF-Token"])
            yield app, client, private
        finally:
            await app.state.container.aclose()


async def test_empty_sso_then_create_then_me_and_no_duplicate(first_entry):
    app, client, _ = first_entry
    response = await client.post(PATH, json=BODY, headers=CSRF)
    assert response.status_code == 201, response.text
    business = response.json()
    assert business["name"] == "Mi negocio"
    uuid.UUID(business["business_id"])
    me = await client.get("/api/v1/auth/me")
    assert me.headers["cache-control"] == "no-store"
    assert me.json()["businesses"] == [business]
    for body in (BODY, {**BODY, "name": "Otro", "reference_currency": "USD"}):
        repeated = await client.post(PATH, json=body, headers=CSRF)
        assert repeated.status_code == 409, repeated.text
        assert repeated.json()["error"]["code"] == "BUSINESS_ALREADY_CONFIGURED"
    async with app.state.container.session_factory() as db:
        row = (
            await db.execute(text("SELECT name,timezone,reference_currency FROM businesses"))
        ).one()
        assert tuple(row) == ("Mi negocio", "Europe/Madrid", "EUR")
        assert (await db.execute(text("SELECT count(*) FROM platform_accounts"))).scalar_one() == 0


async def test_session_and_csrf_remain_required(first_entry):
    app, client, _ = first_entry
    assert (await client.post(PATH, json=BODY)).status_code == 403
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://test"
    ) as anonymous:
        anonymous.cookies.set("ads_csrf", CSRF["X-CSRF-Token"])
        assert (await anonymous.post(PATH, json=BODY, headers=CSRF)).status_code == 401
    async with app.state.container.session_factory() as db:
        assert (await db.execute(text("SELECT count(*) FROM businesses"))).scalar_one() == 0


async def test_new_business_currency_is_not_replaced_by_empty_portfolio_default(first_entry):
    _, client, _ = first_entry
    created = await client.post(PATH, json={**BODY, "reference_currency": "USD"}, headers=CSRF)
    assert created.status_code == 201, created.text
    business_id = created.json()["business_id"]
    cockpit = await client.get("/api/v1/cockpit", params={"business_id": business_id})
    assert cockpit.status_code == 200, cockpit.text
    assert cockpit.json()["currency"] == "USD"


async def test_rollback_leaves_first_entry_available_and_wrong_owner_cannot_claim(first_entry):
    app, client, _ = first_entry
    owner_id = uuid.UUID((await client.get("/api/v1/auth/me")).json()["owner_id"])
    async with app.state.container.session_factory() as db:
        with pytest.raises(OwnerConfigurationAmbiguousError):
            await CreateInitialBusiness(SqlInitialBusinessRepository(db)).execute(
                owner_id=uuid.uuid4(),
                **BODY,
            )
        await db.rollback()
        await CreateInitialBusiness(SqlInitialBusinessRepository(db)).execute(
            owner_id=owner_id,
            **BODY,
        )
        await db.rollback()
    assert (await client.get("/api/v1/auth/me")).json()["businesses"] == []
    assert (await client.post(PATH, json=BODY, headers=CSRF)).status_code == 201


async def test_owner_insert_cannot_cross_initial_owner_count_lock(first_entry):
    app, client, _ = first_entry
    owner_id = uuid.UUID((await client.get("/api/v1/auth/me")).json()["owner_id"])
    async with app.state.container.session_factory() as db:
        await CreateInitialBusiness(SqlInitialBusinessRepository(db)).execute(
            owner_id=owner_id,
            **BODY,
        )
        async with app.state.container.session_factory() as contender:
            await contender.execute(text("SET LOCAL lock_timeout = '100ms'"))
            with pytest.raises(DBAPIError, match="lock timeout"):
                await contender.execute(
                    text("INSERT INTO owners(id,email,password_hash) VALUES(:id,:email,:hash)"),
                    {"id": uuid.uuid4(), "email": "racing@example.test", "hash": "synthetic"},
                )
            await contender.rollback()
        await db.rollback()
    assert (await client.post(PATH, json=BODY, headers=CSRF)).status_code == 201


async def test_two_live_sso_sessions_race_only_one_effect(first_entry):
    app, first, private = first_entry
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://test"
    ) as second:
        response = await second.post(
            "/api/v1/auth/exchange", json={"assertion": _sign(private, _payload())}
        )
        assert response.status_code == 200
        second.cookies.set("ads_csrf", CSRF["X-CSRF-Token"])
        results = await asyncio.gather(
            first.post(PATH, json=BODY, headers=CSRF),
            second.post(PATH, json={**BODY, "name": "Segundo"}, headers=CSRF),
        )
        assert sorted(result.status_code for result in results) == [201, 409]
        first_me = (await first.get("/api/v1/auth/me")).json()["businesses"]
        second_me = (await second.get("/api/v1/auth/me")).json()["businesses"]
        assert len(first_me) == 1 and first_me == second_me


async def test_ambiguous_owner_denied_without_creating_business(first_entry):
    app, client, private = first_entry
    async with app.state.container.session_factory() as db:
        await db.execute(
            text("INSERT INTO owners(id,email,password_hash) VALUES(:id,:email,:hash)"),
            {"id": uuid.uuid4(), "email": "second@example.test", "hash": "not-a-login-hash"},
        )
        await db.commit()
    response = await client.post(PATH, json=BODY, headers=CSRF)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "OWNER_CONFIGURATION_AMBIGUOUS"
    response = await client.post(
        "/api/v1/auth/exchange", json={"assertion": _sign(private, _payload(sub="other-user"))}
    )
    assert response.status_code in (401, 403)
    async with app.state.container.session_factory() as db:
        assert (await db.execute(text("SELECT count(*) FROM businesses"))).scalar_one() == 0


@pytest.mark.parametrize(
    "override",
    [
        {"name": " "},
        {"name": "\nNegocio"},
        {"name": "x\u0000y"},
        {"name": "x" * 121},
        {"name": 3},
        {"timezone": "Not/AZone"},
        {"timezone": "../UTC"},
        {"reference_currency": "eur"},
        {"reference_currency": "EÜR"},
        {"owner_id": str(uuid.uuid4())},
        {"business_id": str(uuid.uuid4())},
    ],
)
async def test_invalid_or_authority_fields_never_persist(first_entry, override):
    app, client, _ = first_entry
    response = await client.post(PATH, json={**BODY, **override}, headers=CSRF)
    assert response.status_code == 422, response.text
    async with app.state.container.session_factory() as db:
        assert (await db.execute(text("SELECT count(*) FROM businesses"))).scalar_one() == 0


async def test_managed_composition_has_no_local_business_registration():
    app = create_app(managed_settings())
    try:
        assert PATH not in {getattr(route, "path", "") for route in app.routes}
    finally:
        await app.state.container.aclose()
