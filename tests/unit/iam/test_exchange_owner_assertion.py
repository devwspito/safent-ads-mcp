"""`ExchangeOwnerAssertion` (026, tasks.md T002, contracts/sso.md §4):
orquestación pura contra dobles en memoria -- la verificación de firma
real vive en `test_ed25519_assertion_verifier.py`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.iam.application.errors import (
    AssertionExpiredError,
    AssertionInvalidError,
    AssertionReplayedError,
    OwnerBoundElsewhereError,
)
from safent_ads.iam.application.exchange_owner_assertion import (
    EXPECTED_AUDIENCE,
    EXPECTED_ISSUER,
    EXPECTED_PURPOSE,
    EXPECTED_VERSION,
    ExchangeOwnerAssertion,
)
from safent_ads.iam.application.ports import (
    AssertionPayload,
    OwnerBridgeResolution,
    ResolvedBridgeOwner,
)
from safent_ads.iam.infrastructure.in_memory_session_repository import InMemorySessionRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import UuidIdGenerator

_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
_SLUG = "safent-ads"
_OWNER_ID = uuid.uuid4()


def _payload(**overrides: object) -> AssertionPayload:
    base: dict[str, object] = {
        "v": EXPECTED_VERSION,
        "iss": EXPECTED_ISSUER,
        "aud": EXPECTED_AUDIENCE,
        "slug": _SLUG,
        "sub": "sub-owner-1",
        "jti": "jti-1",
        "iat": int(_NOW.timestamp()),
        "exp": int((_NOW + timedelta(seconds=60)).timestamp()),
        "purpose": EXPECTED_PURPOSE,
        "surface": "safent_cockpit",
    }
    base.update(overrides)
    return AssertionPayload(**base)  # type: ignore[arg-type]


class _FakeVerifier:
    def __init__(self, payload: AssertionPayload) -> None:
        self._payload = payload

    def verify(self, assertion: str) -> AssertionPayload:
        del assertion
        return self._payload


class _RaisingVerifier:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def verify(self, assertion: str) -> AssertionPayload:
        del assertion
        raise self._error


class _FakeReplayGuard:
    def __init__(self, *, already_seen: frozenset[str] = frozenset()) -> None:
        self._seen: set[str] = set(already_seen)

    async def claim(self, *, jti: str, seen_at: datetime) -> bool:
        del seen_at
        if jti in self._seen:
            return False
        self._seen.add(jti)
        return True


class _FakeOwnerBridge:
    def __init__(
        self,
        *,
        owner_id: uuid.UUID = _OWNER_ID,
        resolution: OwnerBridgeResolution = OwnerBridgeResolution.BOUND,
        error: Exception | None = None,
    ) -> None:
        self._owner_id = owner_id
        self._resolution = resolution
        self._error = error

    async def resolve_for_subject(self, sub: str) -> ResolvedBridgeOwner:
        del sub
        if self._error is not None:
            raise self._error
        return ResolvedBridgeOwner(owner_id=self._owner_id, resolution=self._resolution)


def _use_case(
    *,
    verifier: object,
    replay_guard: object | None = None,
    owner_bridge: object | None = None,
    clock: FixedClock | None = None,
) -> ExchangeOwnerAssertion:
    return ExchangeOwnerAssertion(
        verifier=verifier,  # type: ignore[arg-type]
        replay_guard=replay_guard or _FakeReplayGuard(),  # type: ignore[arg-type]
        owner_bridge=owner_bridge or _FakeOwnerBridge(),  # type: ignore[arg-type]
        session_repository=InMemorySessionRepository(),
        id_generator=UuidIdGenerator(),
        clock=clock or FixedClock(_NOW),
        expected_slug=_SLUG,
    )


async def test_forged_signature_propagates_as_assertion_invalid() -> None:
    use_case = _use_case(verifier=_RaisingVerifier(AssertionInvalidError("firma invalida")))

    with pytest.raises(AssertionInvalidError):
        await use_case.execute(assertion="forged")


async def test_repeated_jti_is_rejected_as_replayed() -> None:
    payload = _payload()
    guard = _FakeReplayGuard(already_seen=frozenset({payload.jti}))
    use_case = _use_case(verifier=_FakeVerifier(payload), replay_guard=guard)

    with pytest.raises(AssertionReplayedError):
        await use_case.execute(assertion="valid")


async def test_expired_assertion_is_rejected() -> None:
    payload = _payload(
        iat=int((_NOW - timedelta(minutes=5)).timestamp()),
        exp=int((_NOW - timedelta(minutes=4)).timestamp()),
    )
    use_case = _use_case(verifier=_FakeVerifier(payload))

    with pytest.raises(AssertionExpiredError):
        await use_case.execute(assertion="valid")


async def test_issued_in_the_future_beyond_skew_is_rejected() -> None:
    payload = _payload(
        iat=int((_NOW + timedelta(minutes=1)).timestamp()),
        exp=int((_NOW + timedelta(minutes=2)).timestamp()),
    )
    use_case = _use_case(verifier=_FakeVerifier(payload))

    with pytest.raises(AssertionExpiredError):
        await use_case.execute(assertion="valid")


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "some-other-audience"},
        {"slug": "some-other-companion"},
        {"iss": "not-safent-runtime"},
        {"purpose": "not-cockpit-session"},
        {"v": 2},
    ],
)
async def test_foreign_literal_fields_are_rejected(overrides: dict[str, object]) -> None:
    payload = _payload(**overrides)
    use_case = _use_case(verifier=_FakeVerifier(payload))

    with pytest.raises(AssertionInvalidError):
        await use_case.execute(assertion="valid")


async def test_owner_bound_elsewhere_propagates() -> None:
    payload = _payload()
    bridge = _FakeOwnerBridge(error=OwnerBoundElsewhereError("atado a otro"))
    use_case = _use_case(verifier=_FakeVerifier(payload), owner_bridge=bridge)

    with pytest.raises(OwnerBoundElsewhereError):
        await use_case.execute(assertion="valid")


async def test_successful_exchange_issues_a_session_for_the_resolved_owner() -> None:
    payload = _payload()
    bridge = _FakeOwnerBridge(owner_id=_OWNER_ID, resolution=OwnerBridgeResolution.CREATED)
    use_case = _use_case(verifier=_FakeVerifier(payload), owner_bridge=bridge)

    authenticated = await use_case.execute(assertion="valid")

    assert authenticated.session.owner_id == _OWNER_ID
    assert authenticated.raw_token
