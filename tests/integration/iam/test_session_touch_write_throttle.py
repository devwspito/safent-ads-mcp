"""Perf (16-sep, item 2, measured on ads.example.com): `GET
/api/v1/auth/me` (and every other authenticated route through
`current_owner`, `iam/presentation/dependencies.py`) issued an
`UPDATE sessions` on every single call. The panel polls several endpoints
every 10-60 s, so the DB paid a write every few seconds for no reason.
`Session.touch` now only asks for persistence when `expires_at` would move
by more than `TOUCH_PERSISTENCE_THRESHOLD` (60 s) -- these tests exercise
the real route end-to-end, over a real Postgres, and count the actual
`UPDATE sessions` statements on the wire."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import event
from tests.integration.iam.test_login_lockout_persists import (
    _CORRECT_PASSWORD,
    _api_settings,
    _client_with_csrf,
)
from tests.integration.iam.test_login_lockout_persists import (
    seeded_owner as seeded_owner,  # noqa: PLC0414 - pytest fixture re-export
)

from safent_ads.composition.app import create_app
from safent_ads.iam.application.session_issuance import hash_session_token
from safent_ads.iam.application.session_policy import SESSION_IDLE_TTL
from safent_ads.iam.domain.session import TOUCH_PERSISTENCE_THRESHOLD
from safent_ads.iam.infrastructure.sql_session_repository import SqlSessionRepository
from safent_ads.shared.clock import FixedClock

pytestmark = pytest.mark.integration

_ME_PATH = "/api/v1/auth/me"


@contextmanager
def _count_session_updates(container):
    sync_engine = container.session_factory.kw["bind"].sync_engine
    statements: list[str] = []

    def before_cursor_execute(_conn, _cursor, statement, _params, _context, _many):
        if statement.strip().upper().startswith("UPDATE SESSIONS"):
            statements.append(statement)

    event.listen(sync_engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield statements
    finally:
        event.remove(sync_engine, "before_cursor_execute", before_cursor_execute)


async def _stored_expires_at(container, raw_token: str):
    async with container.session_factory() as session:
        stored = await SqlSessionRepository(session).get_by_token_hash(
            hash_session_token(raw_token)
        )
    assert stored is not None
    return stored.expires_at


async def _poll_me(client) -> None:
    response = await client.get(_ME_PATH)
    assert response.status_code == 200, response.text


@pytest.fixture
async def session_touch_context(database_url: str, seeded_owner):
    app = create_app(_api_settings(database_url))
    try:
        async with _client_with_csrf(app) as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={"email": seeded_owner.email, "password": _CORRECT_PASSWORD},
            )
            assert login.status_code == 204, login.text
            raw_token = client.cookies["ads_session"]
            container = app.state.container
            # Ancla el reloj a la ultima escritura real (la del login), para
            # controlar de aqui en adelante cuanto se mueve `expires_at` en
            # cada sondeo sin depender del reloj del sistema.
            baseline_expires_at = await _stored_expires_at(container, raw_token)
            clock = FixedClock(baseline_expires_at - SESSION_IDLE_TTL)
            container.clock = clock
            yield client, container, raw_token, clock
    finally:
        await app.state.container.aclose()


async def test_polls_within_the_threshold_never_write_the_session(session_touch_context) -> None:
    client, container, _raw_token, clock = session_touch_context

    with _count_session_updates(container) as updates:
        await _poll_me(client)  # same instant as the stored expires_at: no movement
        clock.advance_to(clock.now() + timedelta(seconds=30))
        await _poll_me(client)  # +30s: below the 60s threshold

    assert updates == []


async def test_poll_past_the_threshold_writes_once_and_keeps_expiry_moving(
    session_touch_context,
) -> None:
    client, container, raw_token, clock = session_touch_context
    before = await _stored_expires_at(container, raw_token)

    with _count_session_updates(container) as updates:
        clock.advance_to(clock.now() + TOUCH_PERSISTENCE_THRESHOLD + timedelta(seconds=1))
        await _poll_me(client)

    assert len(updates) == 1
    after = await _stored_expires_at(container, raw_token)
    assert after == clock.now() + SESSION_IDLE_TTL
    assert after > before


async def test_write_throttle_can_expire_a_session_up_to_the_threshold_earlier(
    session_touch_context,
) -> None:
    """Documented trade-off: an unwritten touch never reaches the database,
    so the stored deadline can lag up to `TOUCH_PERSISTENCE_THRESHOLD`
    behind the freshly-computed one -- a session can expire up to 60 s
    EARLIER than a fully-persisted idle window would, never later."""
    client, container, raw_token, clock = session_touch_context
    stored_before = await _stored_expires_at(container, raw_token)

    with _count_session_updates(container) as updates:
        drift = TOUCH_PERSISTENCE_THRESHOLD - timedelta(seconds=1)
        clock.advance_to(clock.now() + drift)
        await _poll_me(client)  # still below threshold: no write

    assert updates == []
    true_up_to_date_deadline = clock.now() + SESSION_IDLE_TTL
    assert true_up_to_date_deadline - stored_before == drift

    # A request just 1s after the STORED (stale) deadline is rejected even
    # though the true, up-to-date deadline computed from the poll above is
    # still `drift` in the future.
    clock.advance_to(stored_before + timedelta(seconds=1))
    response = await client.get(_ME_PATH)

    assert response.status_code == 401
    assert clock.now() < true_up_to_date_deadline
