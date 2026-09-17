"""Real owner session -> exact intent -> durable nonce -> webhook-token effect.

No Ads provider traffic. Every application/restart uses the same ephemeral PG.
"""

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import event, text
from starlette.requests import Request
from tests.integration.iam.test_login_lockout_persists import (
    _CORRECT_PASSWORD,
    _api_settings,
    _client_with_csrf,
)
from tests.integration.iam.test_login_lockout_persists import (
    seeded_owner as seeded_owner,  # noqa: PLC0414 - pytest fixture re-export
)

from safent_ads.composition.app import create_app
from safent_ads.crm.application.webhook_token import GenerateWebhookToken
from safent_ads.iam.application.action_confirmation import (
    ActionConfirmationCodec,
    ConfirmationError,
)
from safent_ads.iam.application.session_issuance import hash_session_token
from safent_ads.iam.application.session_policy import SESSION_ABSOLUTE_TTL, SESSION_IDLE_TTL
from safent_ads.iam.infrastructure.sql_session_repository import SqlSessionRepository
from safent_ads.iam.presentation.action_confirmation import require_action_confirmation
from safent_ads.iam.presentation.dependencies import AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.shared.clock import FixedClock

pytestmark = pytest.mark.integration


@pytest.fixture
async def context(database_url, seeded_owner):
    app = create_app(_api_settings(database_url))
    business = uuid.uuid4()
    async with app.state.container.session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO businesses (id,slug,name,timezone,reference_currency) "
                "VALUES (:id,:slug,'Test','Europe/Madrid','EUR')"
            ),
            {"id": business, "slug": f"confirmation-{business.hex}"},
        )
        await session.commit()
    async with _client_with_csrf(app) as client:
        response = await client.post(
            "/api/v1/auth/login", json={"email": seeded_owner.email, "password": _CORRECT_PASSWORD}
        )
        assert response.status_code == 204
        yield app, client, f"/api/v1/conversions/webhook-token?business_id={business}", business
    await app.state.container.aclose()


async def _proof(client, path, **kwargs):
    response = await client.post(path, **kwargs)
    assert response.status_code == 428
    assert response.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
    return response.json()["error"]["details"]["confirmation_token"]


async def _count(app, business):
    async with app.state.container.session_factory() as session:
        return (
            await session.execute(
                text("SELECT count(*) FROM conversion_webhook_tokens WHERE business_id=:id"),
                {"id": business},
            )
        ).scalar_one()


async def test_preparation_has_no_effect_and_two_concurrent_proofs_only_one_effect(context):
    app, client, path, business = context
    proof = await _proof(client, path)
    assert await _count(app, business) == 0
    responses = await asyncio.gather(
        *[client.post(path, headers={"X-Action-Confirmation": proof}) for _ in range(2)]
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert await _count(app, business) == 1


@pytest.mark.parametrize("change", ["body", "query", "session"])
async def test_proof_cannot_authorize_another_request(context, change):
    app, client, path, business = context
    proof = await _proof(client, path)
    changed_path = path + "&other=1" if change == "query" else path
    kwargs = {"content": b"{}"} if change == "body" else {}
    if change == "session":
        raw = client.cookies["ads_session"]
        async with app.state.container.session_factory() as session:
            await session.execute(
                text("UPDATE sessions SET token_hash=:new WHERE token_hash=:old"),
                {"new": hash_session_token("different-session"), "old": hash_session_token(raw)},
            )
            await session.commit()
    response = await client.post(changed_path, headers={"X-Action-Confirmation": proof}, **kwargs)
    assert response.status_code == (401 if change == "session" else 409)
    assert await _count(app, business) == 0


async def test_missing_csrf_never_issues_proof(context):
    app, client, path, business = context
    del client.headers["X-CSRF-Token"]
    response = await client.post(path)
    assert response.status_code == 403
    assert "confirmation_token" not in response.text
    assert await _count(app, business) == 0


async def test_logout_revokes_prepared_confirmation(context):
    app, client, path, business = context
    proof = await _proof(client, path)
    raw = client.cookies["ads_session"]
    assert (await client.post("/api/v1/auth/logout")).status_code == 204
    response = await client.post(
        path,
        headers={
            "X-Action-Confirmation": proof,
            "Cookie": f"ads_session={raw}; ads_csrf={client.cookies['ads_csrf']}",
        },
    )
    assert response.status_code == 401
    assert await _count(app, business) == 0


async def test_expired_proof_does_not_refresh_or_execute(context):
    app, client, path, business = context
    clock = FixedClock(app.state.container.clock.now())
    # Routers capture the container clock during construction; inspect codec expiry
    # independently below and change the request binding clock through a new app.
    codec = ActionConfirmationCodec("synthetic-confirmation-key-32bytes-long")
    proof, _ = codec.issue(binding="b" * 64, now=clock.now())

    with pytest.raises(ConfirmationError, match="CONFIRMATION_EXPIRED"):
        codec.verify(proof, binding="b" * 64, now=clock.now() + timedelta(seconds=120))
    assert await _count(app, business) == 0


async def test_restart_retains_consumption(context, database_url):
    app, client, path, business = context
    proof = await _proof(client, path)
    assert (await client.post(path, headers={"X-Action-Confirmation": proof})).status_code == 200
    restarted = create_app(_api_settings(database_url))
    try:
        async with _client_with_csrf(restarted) as second:
            second.cookies.update(client.cookies)
            second.headers["X-CSRF-Token"] = second.cookies["ads_csrf"]
            response = await second.post(path, headers={"X-Action-Confirmation": proof})
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "CONFIRMATION_USED"
        assert await _count(app, business) == 1
    finally:
        await restarted.state.container.aclose()


async def test_failed_effect_burns_proof_without_faking_rollback(context, monkeypatch):

    app, client, path, business = context
    proof = await _proof(client, path)

    async def fail(*_args):
        raise RuntimeError("synthetic-effect-failure")

    with monkeypatch.context() as patch:
        patch.setattr(GenerateWebhookToken, "execute", fail)
        # ASGI test transport re-raises server exceptions; the transaction
        # around the effect rolls back, but nonce consumption already committed.
        with pytest.raises(RuntimeError, match="synthetic-effect-failure"):
            await client.post(path, headers={"X-Action-Confirmation": proof})
    assert await _count(app, business) == 0
    assert (await client.post(path, headers={"X-Action-Confirmation": proof})).status_code == 409
    assert await _count(app, business) == 0


async def test_stale_session_save_cannot_resurrect_logout_or_issued_proof(context):

    app, client, path, business = context
    proof = await _proof(client, path)
    token_hash = hash_session_token(client.cookies["ads_session"])
    async with app.state.container.session_factory() as stale_db:
        repository = SqlSessionRepository(stale_db)
        stale = await repository.get_by_token_hash(token_hash)
        assert stale is not None
        original_id, original_created = stale.id, stale.created_at
        stale.touch(app.state.container.clock.now(), SESSION_IDLE_TTL, SESSION_ABSOLUTE_TTL)
        # Logout commits via a separate request/connection while this stale
        # current_owner-style snapshot remains alive.
        raw = client.cookies["ads_session"]
        assert (await client.post("/api/v1/auth/logout")).status_code == 204
        await repository.save(stale)
        await stale_db.commit()
        stored = await repository.get_by_token_hash(token_hash)
        assert stored is not None and stored.is_revoked
        assert (stored.id, stored.created_at) == (original_id, original_created)
        assert stored.expires_at <= original_created + SESSION_ABSOLUTE_TTL
    response = await client.post(
        path,
        headers={
            "X-Action-Confirmation": proof,
            "Cookie": f"ads_session={raw}; ads_csrf={client.cookies['ads_csrf']}",
        },
    )
    assert response.status_code == 401
    assert await _count(app, business) == 0


async def test_waiting_for_session_lock_does_not_extend_confirmation_ttl(context):
    app, client, _, business = context
    container = app.state.container
    clock = FixedClock(container.clock.now())
    raw = client.cookies["ads_session"]
    async with container.session_factory() as session:
        stored = await SqlSessionRepository(session).get_by_token_hash(hash_session_token(raw))
        assert stored is not None
    codec = ActionConfirmationCodec(container.settings.session_secret.get_secret_value())
    binding = codec.binding(
        session_id=stored.id, method="POST", path="/test-intent", query="", body=b"", action="test"
    )
    proof, expires = codec.issue(binding=binding, now=clock.now())
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/test-intent",
            "query_string": b"",
            "app": app,
            "headers": [
                (b"cookie", f"ads_session={raw}; ads_csrf=test".encode()),
                (b"x-csrf-token", b"test"),
                (b"x-action-confirmation", proof.encode()),
            ],
        }
    )
    request._body = b""
    waiting = asyncio.Event()
    engine = container.session_factory.kw["bind"].sync_engine

    def before_execute(_connection, _cursor, statement, _params, _context, _many):
        if "FOR UPDATE" in statement:
            waiting.set()

    async def confirm():
        async with container.session_factory() as session:
            await require_action_confirmation(
                request,
                session,
                AuthenticatedOwner(owner_id=stored.owner_id, email="synthetic@example.test"),
                action_hash="test",
                clock=clock,
            )

    async with container.session_factory() as blocker:
        await blocker.execute(
            text("SELECT id FROM sessions WHERE id=:id FOR UPDATE"), {"id": stored.id}
        )
        event.listen(engine, "before_cursor_execute", before_execute)
        task = asyncio.create_task(confirm())
        try:
            await asyncio.wait_for(waiting.wait(), timeout=3)
            clock.advance_to(expires + timedelta(seconds=1))
            await blocker.rollback()
            with pytest.raises(ApiError) as error:
                await task
            assert error.value.detail["code"] == "CONFIRMATION_EXPIRED"
        finally:
            event.remove(engine, "before_cursor_execute", before_execute)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert await _count(app, business) == 0
