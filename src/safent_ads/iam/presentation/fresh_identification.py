"""`require_fresh_identification` (spec 002b research.md Decision B,
threat-model.md C-71, T064 security review C-81): the SINGLE point that
decides what counts as presence for a sensitive action, replacing the
direct call to `reauth.py::require_reauth` on `mcp_oauth`'s consent/grants
routers.

Fail-closed order, no exception:
1. `X-Reauth-Token` present -> delegate VERBATIM to
   `reauth.py::require_reauth` (untouched: same `time_step` burn, same
   5/15-min lockout, same audit trail -- 002's behaviour, unchanged).
2. Otherwise, per-ACTION TOTP evidence: a `totp_reauth_confirmations` row
   for THIS `(owner_id, action_hash)` confirmed within
   `FEDERATED_IDENTIFICATION_TTL` -- the exact action the caller is about
   to perform, never a different one.
3. Otherwise, session-level federated freshness
   (`Session.has_fresh_federated_identification`, which itself refuses a
   revoked or expired session) AND a federated identity actually bound to
   this owner, only when `federated_available`.
4. Otherwise, `401 REAUTH_REQUIRED` with `details.methods` -- the vias
   THIS owner can really use (`["totp"]`, `["federated"]`, or both).

C-81 (2026-09-16, T064 security review -- FAIL on the prior design, fixed
here): presence is read according to HOW it was earned, never blended into
one session-wide mark.
- Federated identification is legitimately SESSION-level: proving you are
  that Google account has nothing to do with any one action, so one
  identification covers the whole session for the window (like it did
  before 002b touched this file at all -- `find_for_owner` restored).
- TOTP is legitimately per-ACTION: `reauth.py::require_reauth` already
  keys the RFC 6238 burn by `action_hash` in `totp_reauth_confirmations`
  (0020_totp_replay_guard); reading it back the SAME way -- instead of
  also writing a session-wide `last_federated_auth_at` mark, as this file
  did between T060 and T064 -- means a code that confirmed action A never
  satisfies a DIFFERENT action B. The revoke confirmation dance
  (`grants_router.py`) still closes with a SINGLE code: the confirming
  second POST is the SAME action (identical `action_hash`), so the row the
  first POST just wrote already satisfies step 2 above, no header needed.
- Consequence, verified by test: a federated mark that is fresh but
  earned before the switch flipped off never leaks through for a dueno
  who currently has no viable method (`_viable_methods` gates every read,
  not just the `details` shown on failure) -- decision 3, byte-for-byte.

C-83: this module makes ONE kind of read (session, or
`totp_reauth_confirmations`) and no write of its own -- `require_reauth`
keeps its own pre-existing self-contained commit (0002's behaviour,
untouched); nothing here commits the caller's unit of work.

`methods` is computed from the SAME `federated_available` flag the caller
uses to decide whether `federated_router.py` is even registered (C-71.iv):
this never offers a method that would answer 404. When `methods` would be
empty (federated off and the owner has no TOTP), this delegates to
`require_reauth` one more time instead of inventing a new message -- that
call raises the exact same `REAUTH_REQUIRED` (no `details`) that 002
already returns for that owner, byte for byte."""

from __future__ import annotations

from datetime import datetime

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.iam.application.session_policy import (
    FEDERATED_IDENTIFICATION_TTL,
    SESSION_ABSOLUTE_TTL,
)
from safent_ads.iam.domain.session import Session
from safent_ads.iam.infrastructure.sql_owner_federated_identity_repository import (
    SqlOwnerFederatedIdentityRepository,
)
from safent_ads.iam.infrastructure.sql_owner_repository import SqlOwnerRepository
from safent_ads.iam.presentation.dependencies import AuthenticatedOwner, current_session
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.iam.presentation.reauth import require_reauth
from safent_ads.shared.clock import Clock

_REAUTH_TOKEN_HEADER = "X-Reauth-Token"  # noqa: S105 - header name, not a secret

# 0020_totp_replay_guard: same table `require_reauth` burns into, read back
# by the exact pair it is keyed on. `LIMIT 1`: existence is all this needs.
_RECENT_TOTP_CONFIRMATION_SQL = text("""
    SELECT 1 FROM totp_reauth_confirmations
     WHERE owner_id = :owner_id AND action_hash = :action_hash AND confirmed_at >= :since
     LIMIT 1
""")


async def require_fresh_identification(
    request: Request,
    db_session: AsyncSession,
    owner: AuthenticatedOwner,
    *,
    action_hash: str,
    totp_enc_key: str,
    clock: Clock,
    federated_available: bool,
) -> None:
    if request.headers.get(_REAUTH_TOKEN_HEADER):
        await require_reauth(
            request,
            db_session,
            owner,
            action_hash=action_hash,
            totp_enc_key=totp_enc_key,
            clock=clock,
        )
        return

    now = clock.now()
    if await _has_recent_totp_confirmation(db_session, owner, action_hash=action_hash, now=now):
        return

    if federated_available and await _has_fresh_federated_identification(
        db_session, request, owner, now
    ):
        return

    methods = await _viable_methods(db_session, owner, federated_available=federated_available)
    if not methods:
        await require_reauth(
            request,
            db_session,
            owner,
            action_hash=action_hash,
            totp_enc_key=totp_enc_key,
            clock=clock,
        )
        return
    raise ApiError(
        status_code=401,
        code="REAUTH_REQUIRED",
        message="Confirma que eres tú para continuar.",
        details={"methods": list(methods)},
    )


async def _has_recent_totp_confirmation(
    db_session: AsyncSession, owner: AuthenticatedOwner, *, action_hash: str, now: datetime
) -> bool:
    result = await db_session.execute(
        _RECENT_TOTP_CONFIRMATION_SQL,
        {
            "owner_id": str(owner.owner_id),
            "action_hash": action_hash,
            "since": now - FEDERATED_IDENTIFICATION_TTL,
        },
    )
    return result.first() is not None


async def _has_fresh_federated_identification(
    db_session: AsyncSession, request: Request, owner: AuthenticatedOwner, now: datetime
) -> bool:
    session: Session = await current_session(request, db_session)
    if not session.has_fresh_federated_identification(
        now, FEDERATED_IDENTIFICATION_TTL, SESSION_ABSOLUTE_TTL
    ):
        return False
    identity = await SqlOwnerFederatedIdentityRepository(db_session).find_for_owner(
        owner_id=owner.owner_id
    )
    return identity is not None


async def _viable_methods(
    db_session: AsyncSession, owner: AuthenticatedOwner, *, federated_available: bool
) -> tuple[str, ...]:
    methods: list[str] = []
    stored_owner = await SqlOwnerRepository(db_session).get_by_id(owner.owner_id)
    if stored_owner is not None and stored_owner.totp_secret_encrypted is not None:
        methods.append("totp")
    if federated_available:
        identity = await SqlOwnerFederatedIdentityRepository(db_session).find_for_owner(
            owner_id=owner.owner_id
        )
        if identity is not None:
            methods.append("federated")
    return tuple(methods)
