"""`BeginOAuthConnect`: persiste la sesion hasheada, nunca el `state` en
claro, y devuelve `session_id` (no `state`) al llamante."""

from __future__ import annotations

import uuid

from safent_ads.accounts.application.begin_oauth_connect import BeginOAuthConnect
from safent_ads.accounts.application.oauth_state_hash import hash_state
from safent_ads.accounts.testing.in_memory_repositories import (
    InMemoryOAuthConnectSessionRepository,
)
from safent_ads.shared.ids import BusinessId, PlatformCode, UuidIdGenerator
from tests.unit.accounts.application.conftest import FakeOAuthBrokerPort, default_begin_result


async def test_persists_session_hashed_and_never_returns_the_raw_state() -> None:
    begin_result = default_begin_result()
    broker = FakeOAuthBrokerPort(begin_result=begin_result)
    sessions = InMemoryOAuthConnectSessionRepository()
    use_case = BeginOAuthConnect(broker, sessions, UuidIdGenerator())
    business_id = BusinessId.new()
    owner_id = uuid.uuid4()

    result = await use_case.execute(
        provider=PlatformCode.GOOGLE,
        business_id=business_id,
        owner_id=owner_id,
        redirect_uri="https://x/callback",
    )

    assert result.authorize_url == begin_result.authorization_url
    assert result.expires_at == begin_result.expires_at
    assert not hasattr(result, "state")

    stored = await sessions.get_by_id(result.session_id)
    assert stored is not None
    assert stored.state_hash == hash_state(begin_result.state)
    assert stored.business_id == business_id
    assert stored.owner_id == owner_id
    assert stored.provider == PlatformCode.GOOGLE
