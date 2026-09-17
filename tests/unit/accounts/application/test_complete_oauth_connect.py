"""`CompleteOAuthConnect`: resuelve la sesion por `state` (un solo uso,
TTL), crea `PlatformAccount` + `credential_refs` por cuenta descubierta, y
traduce el rechazo del broker sin perder el motivo."""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from safent_ads.accounts.application.complete_oauth_connect import CompleteOAuthConnect
from safent_ads.accounts.application.connect_ports import DiscoveredAccount, OAuthCompleteResult
from safent_ads.accounts.application.errors import (
    BrokerRequestDeniedError,
    OAuthProviderDeniedError,
    OAuthSessionExpiredError,
    OAuthSessionNotFoundError,
)
from safent_ads.accounts.application.oauth_state_hash import hash_state
from safent_ads.accounts.domain.oauth_connect_session import OAuthConnectSession, OAuthSessionStatus
from safent_ads.accounts.domain.platform_account import ApiTier
from safent_ads.accounts.domain.refs import AccountRef, CredentialRefId
from safent_ads.accounts.testing.in_memory_repositories import (
    InMemoryAccountRepository,
    InMemoryCredentialRepository,
    InMemoryOAuthConnectSessionRepository,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, PlatformCode
from tests.unit.accounts.application.conftest import NOW, FakeOAuthBrokerPort

_STATE = "the-raw-state"


def _waiting_session(business_id: BusinessId, **overrides: object) -> OAuthConnectSession:
    defaults: dict[str, object] = {
        "session_id": uuid.uuid4(),
        "business_id": business_id,
        "owner_id": uuid.uuid4(),
        "provider": PlatformCode.GOOGLE,
        "state_hash": hash_state(_STATE),
        "expires_at": NOW + timedelta(minutes=10),
    }
    defaults.update(overrides)
    return OAuthConnectSession(**defaults)  # type: ignore[arg-type]


def _discovered_account(**overrides: object) -> DiscoveredAccount:
    defaults: dict[str, object] = {
        "platform": PlatformCode.GOOGLE,
        "external_account_id": "1234567890",
        "label": "Cliente",
        "currency": "EUR",
        "timezone": "Europe/Madrid",
        "api_tier": ApiTier.GOOGLE_EXPLORER,
        "credential_ref_id": CredentialRefId(uuid.uuid4()),
        "scopes": frozenset({"adwords"}),
        "obtained_at": NOW,
        "expires_at": None,
    }
    defaults.update(overrides)
    return DiscoveredAccount(**defaults)  # type: ignore[arg-type]


def _use_case(
    *,
    business_id: BusinessId,
    broker: FakeOAuthBrokerPort,
    sessions: InMemoryOAuthConnectSessionRepository | None = None,
    accounts: InMemoryAccountRepository | None = None,
    credentials: InMemoryCredentialRepository | None = None,
) -> tuple[CompleteOAuthConnect, InMemoryOAuthConnectSessionRepository]:
    sessions = sessions or InMemoryOAuthConnectSessionRepository([_waiting_session(business_id)])
    use_case = CompleteOAuthConnect(
        broker,
        sessions,
        accounts or InMemoryAccountRepository(),
        credentials or InMemoryCredentialRepository(),
        FixedClock(NOW),
    )
    return use_case, sessions


async def test_unknown_state_raises() -> None:
    business_id = BusinessId.new()
    use_case, _ = _use_case(
        business_id=business_id,
        broker=FakeOAuthBrokerPort(),
        sessions=InMemoryOAuthConnectSessionRepository(),
    )

    with pytest.raises(OAuthSessionNotFoundError):
        await use_case.execute(state="never-began", code="auth-code")


async def test_wrong_provider_does_not_consume_or_exchange_state() -> None:
    use_case, sessions = _use_case(business_id=BusinessId.new(), broker=FakeOAuthBrokerPort())
    with pytest.raises(OAuthSessionNotFoundError):
        await use_case.execute(state=_STATE, code="auth-code", provider=PlatformCode.META)
    assert (
        await sessions.get_by_state_hash(hash_state(_STATE))
    ).status == OAuthSessionStatus.WAITING


async def test_cancellation_consumes_session_without_broker_exchange() -> None:
    use_case, sessions = _use_case(business_id=BusinessId.new(), broker=FakeOAuthBrokerPort())
    assert await use_case.execute(state=_STATE, code=None, provider=PlatformCode.GOOGLE) == []
    assert (await sessions.get_by_state_hash(hash_state(_STATE))).error_code == "OAUTH_CANCELLED"
    with pytest.raises(OAuthSessionNotFoundError):
        await use_case.execute(state=_STATE, code="auth-code", provider=PlatformCode.GOOGLE)


async def test_expired_session_raises() -> None:
    business_id = BusinessId.new()
    expired = _waiting_session(business_id, expires_at=NOW - timedelta(seconds=1))
    use_case, _ = _use_case(
        business_id=business_id,
        broker=FakeOAuthBrokerPort(),
        sessions=InMemoryOAuthConnectSessionRepository([expired]),
    )

    with pytest.raises(OAuthSessionExpiredError):
        await use_case.execute(state=_STATE, code="auth-code")
    assert expired.status == OAuthSessionStatus.ERROR
    assert expired.error_code == "OAUTH_SESSION_EXPIRED"


async def test_inventory_finishing_after_ttl_does_not_admit_accounts() -> None:
    business_id = BusinessId.new()
    session = _waiting_session(business_id)
    clock = FixedClock(NOW)
    accounts = InMemoryAccountRepository()

    class ExpiringBroker(FakeOAuthBrokerPort):
        async def complete(self, state: str, code: str) -> OAuthCompleteResult:
            assert state == _STATE and code == "auth-code"
            session.expires_at = NOW
            return OAuthCompleteResult(accounts=[_discovered_account()])

    use_case = CompleteOAuthConnect(
        ExpiringBroker(), InMemoryOAuthConnectSessionRepository([session]),
        accounts, InMemoryCredentialRepository(), clock,
    )
    with pytest.raises(OAuthSessionExpiredError):
        await use_case.execute(state=_STATE, code="auth-code")
    assert await accounts.list_by_business(business_id) == []
    assert session.error_code == "OAUTH_SESSION_EXPIRED"


async def test_creates_account_and_credential_for_each_discovered_account() -> None:
    business_id = BusinessId.new()
    discovered = _discovered_account()
    accounts = InMemoryAccountRepository()
    credentials = InMemoryCredentialRepository()
    use_case, sessions = _use_case(
        business_id=business_id,
        broker=FakeOAuthBrokerPort(complete_result=OAuthCompleteResult(accounts=[discovered])),
        accounts=accounts,
        credentials=credentials,
    )

    created = await use_case.execute(state=_STATE, code="auth-code")

    assert len(created) == 1
    account_ref = AccountRef(PlatformCode.GOOGLE, "1234567890")
    stored_account = await accounts.get_by_ref(account_ref)
    assert stored_account is not None
    assert stored_account.business_id == business_id
    assert stored_account.credential_ref_id == discovered.credential_ref_id

    stored_credential = await credentials.get(discovered.credential_ref_id, business_id=business_id)
    assert stored_credential is not None
    assert stored_credential.scopes == discovered.scopes

    session = await sessions.get_by_state_hash(hash_state(_STATE))
    assert session is not None
    assert session.status == OAuthSessionStatus.OK


@pytest.mark.parametrize("provider", [PlatformCode.GOOGLE, PlatformCode.META])
async def test_empty_inventory_is_a_terminal_error_without_upserts(provider: PlatformCode) -> None:
    business_id = BusinessId.new()
    session = _waiting_session(business_id, provider=provider, connection_id=uuid.uuid4())
    broker = FakeOAuthBrokerPort(complete_result=OAuthCompleteResult(accounts=[]))
    accounts, credentials = AsyncMock(), AsyncMock()
    sessions = InMemoryOAuthConnectSessionRepository([session])
    use_case = CompleteOAuthConnect(broker, sessions, accounts, credentials, FixedClock(NOW))

    with pytest.raises(OAuthProviderDeniedError, match="OAUTH_NO_ACCESSIBLE_ACCOUNTS"):
        await use_case.execute(state=_STATE, code="auth-code", provider=provider)

    assert session.status == OAuthSessionStatus.ERROR
    assert session.error_code == "OAUTH_NO_ACCESSIBLE_ACCOUNTS"
    assert session.completed_at == NOW
    assert await sessions.get_by_id(session.session_id) is session
    assert accounts.mock_calls == []
    assert credentials.mock_calls == []
    with pytest.raises(OAuthSessionNotFoundError):
        await use_case.execute(state=_STATE, code="auth-code", provider=provider)
    assert broker.received_complete_calls == [(_STATE, "auth-code")]


async def test_empty_inventory_after_ttl_keeps_expiry_error() -> None:
    business_id = BusinessId.new()
    session = _waiting_session(business_id)

    class ExpiringBroker(FakeOAuthBrokerPort):
        async def complete(self, state: str, code: str) -> OAuthCompleteResult:
            assert state == _STATE and code == "auth-code"
            session.expires_at = NOW
            return OAuthCompleteResult(accounts=[])

    use_case = CompleteOAuthConnect(
        ExpiringBroker(), InMemoryOAuthConnectSessionRepository([session]),
        AsyncMock(), AsyncMock(), FixedClock(NOW),
    )
    with pytest.raises(OAuthSessionExpiredError):
        await use_case.execute(state=_STATE, code="auth-code")
    assert session.status == OAuthSessionStatus.ERROR
    assert session.error_code == "OAUTH_SESSION_EXPIRED"


async def test_session_is_single_use() -> None:
    business_id = BusinessId.new()
    discovered = _discovered_account()
    use_case, _ = _use_case(
        business_id=business_id,
        broker=FakeOAuthBrokerPort(complete_result=OAuthCompleteResult(accounts=[discovered])),
    )

    await use_case.execute(state=_STATE, code="auth-code")

    with pytest.raises(OAuthSessionNotFoundError):
        await use_case.execute(state=_STATE, code="auth-code")


@pytest.mark.parametrize(
    "error_code", ["OAUTH_PROVIDER_DENIED", "GOOGLE_PROJECT_ACCESS_LEVEL_TEST"]
)
async def test_broker_denial_marks_session_error_and_raises(error_code: str) -> None:
    business_id = BusinessId.new()
    use_case, sessions = _use_case(
        business_id=business_id,
        broker=FakeOAuthBrokerPort(deny=BrokerRequestDeniedError(error_code)),
    )

    with pytest.raises(OAuthProviderDeniedError):
        await use_case.execute(state=_STATE, code="auth-code")

    session = await sessions.get_by_state_hash(hash_state(_STATE))
    assert session is not None
    assert session.status == OAuthSessionStatus.ERROR
    assert session.error_code == error_code
