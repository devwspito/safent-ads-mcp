"""`ExchangeOwnerAssertion` (026, tasks.md T002): canjea la aserción Ed25519
firmada por el daemon de Safent por una `Session` de `ads-api`
(`POST /api/v1/auth/exchange`, contracts/sso.md §4). Orden fail-closed en
cada paso: firma (puerto `AssertionVerifier`) -> campos literales -> ventana
temporal -> antirrepetición de `jti` -> resolución del propietario (TOFU)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from safent_ads.iam.application.errors import (
    AssertionExpiredError,
    AssertionInvalidError,
    AssertionReplayedError,
)
from safent_ads.iam.application.ports import (
    AssertionPayload,
    AssertionReplayGuard,
    AssertionVerifier,
    OwnerBridgeRepository,
    SessionRepository,
)
from safent_ads.iam.application.session_issuance import AuthenticatedSession, issue_session
from safent_ads.iam.domain.session import SessionOrigin
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import IdGenerator

EXPECTED_VERSION = 1
EXPECTED_ISSUER = "safent-runtime"
EXPECTED_AUDIENCE = "safent-ads"
EXPECTED_PURPOSE = "cockpit_session"
_IAT_CLOCK_SKEW = timedelta(seconds=5)


class ExchangeOwnerAssertion:
    def __init__(
        self,
        *,
        verifier: AssertionVerifier,
        replay_guard: AssertionReplayGuard,
        owner_bridge: OwnerBridgeRepository,
        session_repository: SessionRepository,
        id_generator: IdGenerator,
        clock: Clock,
        expected_slug: str,
    ) -> None:
        self._verifier = verifier
        self._replay_guard = replay_guard
        self._owner_bridge = owner_bridge
        self._sessions = session_repository
        self._id_generator = id_generator
        self._clock = clock
        self._expected_slug = expected_slug

    async def execute(self, *, assertion: str) -> AuthenticatedSession:
        payload = self._verifier.verify(assertion)
        self._require_expected_literals(payload)
        self._require_within_window(payload)
        if not await self._replay_guard.claim(jti=payload.jti, seen_at=self._clock.now()):
            raise AssertionReplayedError(f"jti ya canjeado: {payload.jti}")

        resolved = await self._owner_bridge.resolve_for_subject(payload.sub)
        return await issue_session(
            owner_id=resolved.owner_id,
            sessions=self._sessions,
            id_generator=self._id_generator,
            clock=self._clock,
            origin=SessionOrigin.BRIDGE,
        )

    def _require_expected_literals(self, payload: AssertionPayload) -> None:
        if (
            payload.v != EXPECTED_VERSION
            or payload.iss != EXPECTED_ISSUER
            or payload.aud != EXPECTED_AUDIENCE
            or payload.slug != self._expected_slug
            or payload.purpose != EXPECTED_PURPOSE
        ):
            raise AssertionInvalidError("campos literales de la aserción no coinciden")

    def _require_within_window(self, payload: AssertionPayload) -> None:
        now = self._clock.now()
        issued_at = datetime.fromtimestamp(payload.iat, tz=UTC)
        expires_at = datetime.fromtimestamp(payload.exp, tz=UTC)
        if issued_at > now + _IAT_CLOCK_SKEW or now >= expires_at:
            raise AssertionExpiredError(f"iat={payload.iat} exp={payload.exp} now={now}")
