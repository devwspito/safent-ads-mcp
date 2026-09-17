"""Community password sessions, durable lockout, dummy hashing and logout."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.iam.application.errors import (
    AccountLockedError,
    InvalidCredentialsError,
)
from safent_ads.iam.application.login import Login
from safent_ads.iam.application.logout import Logout
from safent_ads.iam.application.session_issuance import hash_session_token
from safent_ads.iam.application.session_policy import LOCKOUT_THRESHOLD
from safent_ads.iam.domain.email import Email
from safent_ads.iam.domain.owner import Owner
from safent_ads.iam.infrastructure.fakes import (
    FakePasswordHasher,
    FakeTotpCipher,
)
from safent_ads.iam.infrastructure.in_memory_login_attempt_repository import (
    InMemoryLoginAttemptRepository,
)
from safent_ads.iam.infrastructure.in_memory_owner_repository import InMemoryOwnerRepository
from safent_ads.iam.infrastructure.in_memory_session_repository import InMemorySessionRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import UuidIdGenerator

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
_PASSWORD = "correct horse battery staple"  # noqa: S105 - fixture, no secreto real
_IP = "203.0.113.7"


def _enrolled_owner() -> Owner:
    hasher = FakePasswordHasher()
    cipher = FakeTotpCipher()
    owner = Owner(
        owner_id=uuid.uuid4(),
        email=Email("owner@safent.example"),
        password_hash=hasher.hash(_PASSWORD),
        totp_secret_encrypted=cipher.encrypt("SECRET"),
        totp_confirmed_at=_NOW - timedelta(days=1),
        created_at=_NOW - timedelta(days=30),
    )
    return owner


def _build_login(owner: Owner, clock: FixedClock) -> tuple[Login, InMemoryLoginAttemptRepository]:
    owners = InMemoryOwnerRepository([owner])
    attempts = InMemoryLoginAttemptRepository(clock)
    hasher = FakePasswordHasher()
    login = Login(
        owner_repository=owners,
        login_attempt_repository=attempts,
        password_hasher=hasher,
        session_repository=InMemorySessionRepository(),
        id_generator=UuidIdGenerator(),
        clock=clock,
        decoy_password_hash=hasher.hash("decoy"),
    )
    return login, attempts


async def test_login_creates_session_even_with_legacy_totp() -> None:
    owner = _enrolled_owner()
    clock = FixedClock(_NOW)
    login, _ = _build_login(owner, clock)

    challenge = await login.execute(email=str(owner.email), password=_PASSWORD, ip_address=_IP)

    assert challenge.raw_token
    assert challenge.session.expires_at > _NOW


async def test_login_accepts_a_missing_ip_address_without_raising() -> None:
    """Code review 17-sep (item 2): `ip_address` es `None` cuando
    `shared.net.client_ip.resolve_client_ip` no pudo resolver ninguna IP
    valida (`request.client` ausente) -- el caso de uso no debe romperse,
    y el registro de intentos debe aceptar `None` (columna `INET`
    nullable)."""
    owner = _enrolled_owner()
    clock = FixedClock(_NOW)
    login, attempts = _build_login(owner, clock)

    challenge = await login.execute(email=str(owner.email), password=_PASSWORD, ip_address=None)

    assert challenge.raw_token
    assert (
        await attempts.count_recent_failures(
            email=str(owner.email), ip_address=None, since=_NOW - timedelta(minutes=1)
        )
        == 0
    )


async def test_login_rejects_wrong_password() -> None:
    owner = _enrolled_owner()
    clock = FixedClock(_NOW)
    login, attempts = _build_login(owner, clock)

    with pytest.raises(InvalidCredentialsError):
        await login.execute(email=str(owner.email), password="wrong", ip_address=_IP)

    assert (
        await attempts.count_recent_failures(
            email=str(owner.email), ip_address=_IP, since=_NOW - timedelta(minutes=1)
        )
        == 1
    )


async def test_login_rejects_unknown_email_without_leaking_existence() -> None:
    owner = _enrolled_owner()
    clock = FixedClock(_NOW)
    login, _ = _build_login(owner, clock)

    with pytest.raises(InvalidCredentialsError):
        await login.execute(email="nobody@safent.example", password=_PASSWORD, ip_address=_IP)


@pytest.mark.parametrize("known_owner", [False, True])
async def test_password_failure_always_pays_exactly_one_hash_verification(known_owner, monkeypatch):
    owner = _enrolled_owner()
    login, _ = _build_login(owner, FixedClock(_NOW))
    calls = []
    verify = login._password_hasher.verify

    def recorded(password, password_hash):
        calls.append(password_hash)
        return verify(password, password_hash)

    monkeypatch.setattr(login._password_hasher, "verify", recorded)
    with pytest.raises(InvalidCredentialsError):
        await login.execute(
            email=str(owner.email) if known_owner else "nobody@safent.example",
            password="wrong",
            ip_address=_IP,
        )
    expected = owner.password_hash if known_owner else login._decoy_password_hash
    assert calls == [expected]


async def test_login_rejects_malformed_email_instead_of_raising_domain_error() -> None:
    """Regresion: `Login.execute` dejaba escapar `InvalidEmailError` sin
    capturar, lo que reventaba como 500 en el router en vez de 401."""
    owner = _enrolled_owner()
    clock = FixedClock(_NOW)
    login, _ = _build_login(owner, clock)

    with pytest.raises(InvalidCredentialsError):
        await login.execute(email="not-an-email", password=_PASSWORD, ip_address=_IP)


async def test_login_accepts_owner_without_totp_enrolled() -> None:
    owner = Owner(
        owner_id=uuid.uuid4(),
        email=Email("fresh@safent.example"),
        password_hash=FakePasswordHasher().hash(_PASSWORD),
        totp_secret_encrypted=None,
        totp_confirmed_at=None,
        created_at=_NOW,
    )
    clock = FixedClock(_NOW)
    login, _ = _build_login(owner, clock)

    result = await login.execute(email=str(owner.email), password=_PASSWORD, ip_address=_IP)
    assert result.session.owner_id == owner.id
    assert result.session.token_hash == hash_session_token(result.raw_token)


async def test_login_locks_out_after_five_failures_from_same_ip() -> None:
    owner = _enrolled_owner()
    clock = FixedClock(_NOW)
    login, _attempts = _build_login(owner, clock)

    for _ in range(LOCKOUT_THRESHOLD):
        with pytest.raises(InvalidCredentialsError):
            await login.execute(email=str(owner.email), password="wrong", ip_address=_IP)

    with pytest.raises(AccountLockedError):
        await login.execute(email=str(owner.email), password=_PASSWORD, ip_address=_IP)


async def test_login_lockout_is_scoped_by_ip_address() -> None:
    owner = _enrolled_owner()
    clock = FixedClock(_NOW)
    login, _attempts = _build_login(owner, clock)

    for _ in range(LOCKOUT_THRESHOLD):
        with pytest.raises(InvalidCredentialsError):
            await login.execute(email=str(owner.email), password="wrong", ip_address=_IP)

    challenge = await login.execute(
        email=str(owner.email), password=_PASSWORD, ip_address="198.51.100.9"
    )
    assert challenge.raw_token


async def test_logout_revokes_the_session() -> None:
    owner = _enrolled_owner()
    clock = FixedClock(_NOW)
    login, _ = _build_login(owner, clock)
    authenticated = await login.execute(email=str(owner.email), password=_PASSWORD, ip_address=_IP)
    sessions = login._sessions
    logout = Logout(sessions, clock)
    await logout.execute(raw_token=authenticated.raw_token)

    stored = await sessions.get_by_token_hash(authenticated.session.token_hash)
    assert stored is not None
    assert stored.is_revoked is True


async def test_logout_is_idempotent_for_unknown_token() -> None:
    sessions = InMemorySessionRepository()
    logout = Logout(sessions, FixedClock(_NOW))

    await logout.execute(raw_token="never-issued-token")
