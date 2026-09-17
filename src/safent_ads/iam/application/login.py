"""Community owner password login. Enterprise MFA lives in Enterprise.

Password failures remain durable and rate limited; unknown owners pay the
same password-hash verification cost as known owners.
"""

from __future__ import annotations

from safent_ads.iam.application.errors import AccountLockedError, InvalidCredentialsError
from safent_ads.iam.application.ports import (
    LoginAttemptRepository,
    OwnerRepository,
    PasswordHasher,
    SessionRepository,
)
from safent_ads.iam.application.session_issuance import AuthenticatedSession, issue_session
from safent_ads.iam.application.session_policy import (
    LOCKOUT_THRESHOLD,
    LOCKOUT_WINDOW,
)
from safent_ads.iam.domain.email import Email, InvalidEmailError
from safent_ads.iam.domain.owner import Owner
from safent_ads.iam.domain.session import SessionOrigin
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import IdGenerator


class Login:
    def __init__(
        self,
        *,
        owner_repository: OwnerRepository,
        login_attempt_repository: LoginAttemptRepository,
        password_hasher: PasswordHasher,
        session_repository: SessionRepository,
        id_generator: IdGenerator,
        clock: Clock,
        decoy_password_hash: str,
    ) -> None:
        self._owners = owner_repository
        self._login_attempts = login_attempt_repository
        self._password_hasher = password_hasher
        self._sessions = session_repository
        self._ids = id_generator
        self._clock = clock
        self._decoy_password_hash = decoy_password_hash

    async def execute(
        self, *, email: str, password: str, ip_address: str | None
    ) -> AuthenticatedSession:
        parsed_email = self._parse_email(email)
        await self._reject_if_locked(str(parsed_email), ip_address)

        owner = await self._owners.get_by_email(parsed_email)
        password_valid = self._verify_password(owner, password)
        if owner is None or not password_valid:
            await self._login_attempts.record(
                email=str(parsed_email), succeeded=False, ip_address=ip_address
            )
            raise InvalidCredentialsError("credenciales invalidas")

        await self._login_attempts.record(
            email=str(parsed_email), succeeded=True, ip_address=ip_address
        )
        return await issue_session(
            owner_id=owner.id,
            sessions=self._sessions,
            id_generator=self._ids,
            clock=self._clock,
            origin=SessionOrigin.PASSWORD,
        )

    @staticmethod
    def _parse_email(email: str) -> Email:
        try:
            return Email(email)
        except InvalidEmailError as exc:
            raise InvalidCredentialsError("credenciales invalidas") from exc

    async def _reject_if_locked(self, email: str, ip_address: str | None) -> None:
        since = self._clock.now() - LOCKOUT_WINDOW
        failures = await self._login_attempts.count_recent_failures(
            email=email, ip_address=ip_address, since=since
        )
        if failures >= LOCKOUT_THRESHOLD:
            raise AccountLockedError("demasiados intentos recientes")

    def _verify_password(self, owner: Owner | None, password: str) -> bool:
        if owner is None:
            # Ejecuta el mismo coste de Argon2id sobre un hash senuelo para
            # que el tiempo de respuesta no delate si el email existe.
            self._password_hasher.verify(password, self._decoy_password_hash)
            return False
        return self._password_hasher.verify(password, owner.password_hash)
