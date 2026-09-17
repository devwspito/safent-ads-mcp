"""Single session-token issuance boundary for password login, native SSO and
federated login.

256-bit opaque token; only its hash is persisted. Legacy data-encryption
keys remain untouched by the retirement of Community MFA.

Every caller states how the session was born (`origin`) and, when the birth
itself is the proof of presence (Google), when that proof happened
(`federated_auth_at`). The `Session` aggregate refuses a federated origin
without its mark, so there is no way to issue one by accident.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime

from safent_ads.iam.application.ports import SessionRepository
from safent_ads.iam.application.session_policy import SESSION_IDLE_TTL
from safent_ads.iam.domain.session import Session, SessionOrigin
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import IdGenerator

_TOKEN_BYTES = 32  # 256 bits


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    session: Session
    raw_token: str


def hash_session_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


async def issue_session(
    *,
    owner_id: uuid.UUID,
    sessions: SessionRepository,
    id_generator: IdGenerator,
    clock: Clock,
    origin: SessionOrigin,
    federated_auth_at: datetime | None = None,
) -> AuthenticatedSession:
    raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
    now = clock.now()
    session = Session(
        session_id=id_generator.new_id(),
        owner_id=owner_id,
        token_hash=hash_session_token(raw_token),
        created_at=now,
        expires_at=now + SESSION_IDLE_TTL,
        revoked_at=None,
        origin=origin,
        last_federated_auth_at=federated_auth_at,
    )
    await sessions.create(session)
    return AuthenticatedSession(session=session, raw_token=raw_token)
