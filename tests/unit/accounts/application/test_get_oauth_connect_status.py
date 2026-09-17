"""`GetOAuthConnectStatus`: `session_id` no autoriza por si solo."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import timedelta

import pytest

from safent_ads.accounts.application.errors import OAuthSessionNotFoundError
from safent_ads.accounts.application.get_oauth_connect_status import GetOAuthConnectStatus
from safent_ads.accounts.domain.oauth_connect_session import OAuthConnectSession, OAuthSessionStatus
from safent_ads.accounts.testing.in_memory_repositories import (
    InMemoryOAuthConnectSessionRepository,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, PlatformCode
from tests.unit.accounts.application.conftest import NOW


def _session(business_id: BusinessId) -> OAuthConnectSession:
    return OAuthConnectSession(
        session_id=uuid.uuid4(),
        business_id=business_id,
        owner_id=uuid.uuid4(),
        provider=PlatformCode.GOOGLE,
        state_hash="a" * 64,
        expires_at=NOW + timedelta(minutes=10),
    )


async def test_returns_the_session_for_its_own_business() -> None:
    business_id = BusinessId.new()
    session = _session(business_id)
    use_case = GetOAuthConnectStatus(
        InMemoryOAuthConnectSessionRepository([session]), FixedClock(NOW)
    )

    result = await use_case.execute(business_id=business_id, session_id=session.session_id)

    assert result is session


async def test_knowing_the_session_id_alone_is_not_enough() -> None:
    owner_business = BusinessId.new()
    session = _session(owner_business)
    use_case = GetOAuthConnectStatus(
        InMemoryOAuthConnectSessionRepository([session]), FixedClock(NOW)
    )

    with pytest.raises(OAuthSessionNotFoundError):
        await use_case.execute(business_id=BusinessId.new(), session_id=session.session_id)


async def test_unknown_session_id_raises() -> None:
    use_case = GetOAuthConnectStatus(InMemoryOAuthConnectSessionRepository(), FixedClock(NOW))

    with pytest.raises(OAuthSessionNotFoundError):
        await use_case.execute(business_id=BusinessId.new(), session_id=uuid.uuid4())


async def test_expired_waiting_is_persisted_as_terminal_error() -> None:
    business_id = BusinessId.new()
    session = _session(business_id)
    sessions = InMemoryOAuthConnectSessionRepository([session])
    result = await GetOAuthConnectStatus(sessions, FixedClock(session.expires_at)).execute(
        business_id=business_id, session_id=session.session_id,
    )
    assert result.status == OAuthSessionStatus.ERROR
    assert result.error_code == "OAUTH_SESSION_EXPIRED"
    assert (await sessions.get_by_id(session.session_id)).completed_at == session.expires_at


async def test_expiry_rechecks_locked_row_without_overwriting_completed_inventory() -> None:
    business_id = BusinessId.new()
    waiting = _session(business_id)
    completed = replace(waiting, status=OAuthSessionStatus.OK, completed_at=NOW)

    class ConcurrentSessions(InMemoryOAuthConnectSessionRepository):
        async def get_by_state_hash(self, state_hash: str) -> OAuthConnectSession:
            assert state_hash == waiting.state_hash
            return completed

        async def save(self, session: OAuthConnectSession) -> None:
            assert session is not None
            pytest.fail("expiry must not overwrite a callback that committed while locking")

    result = await GetOAuthConnectStatus(
        ConcurrentSessions([waiting]), FixedClock(waiting.expires_at),
    ).execute(business_id=business_id, session_id=waiting.session_id)
    assert result.status == OAuthSessionStatus.OK
