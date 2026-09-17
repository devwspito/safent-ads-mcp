"""Owner + CSRF + exact request + live session + durable one-shot confirmation."""

from datetime import UTC, datetime

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.composition.api import _csrf_token_valid
from safent_ads.iam.application.action_confirmation import (
    ActionConfirmationCodec,
    ConfirmationError,
)
from safent_ads.iam.application.session_issuance import hash_session_token
from safent_ads.iam.application.session_policy import SESSION_ABSOLUTE_TTL
from safent_ads.iam.domain.errors import SessionExpiredError, SessionRevokedError
from safent_ads.iam.infrastructure.sql_session_repository import SqlSessionRepository
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME, AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.shared.clock import Clock


async def require_action_confirmation(
    request: Request,
    session: AsyncSession,
    owner: AuthenticatedOwner,
    *,
    action_hash: str,
    clock: Clock,
) -> str:
    """Returns the nonce of the proof just burned: the one-shot id callers
    can carry as a correlation key into downstream audit records. It is
    already consumed here, so it authorizes nothing further."""
    if not _csrf_token_valid(request):
        raise ApiError(status_code=403, code="CSRF_INVALID", message="Solicitud no verificada.")
    raw_session = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_session:
        raise ApiError(status_code=401, code="UNAUTHORIZED", message="Sesión no válida.")
    # Re-read after scope resolution, including on the confirmed request.
    stored = await SqlSessionRepository(session).get_by_token_hash(hash_session_token(raw_session))
    if stored is None or stored.owner_id != owner.owner_id:
        raise ApiError(status_code=401, code="UNAUTHORIZED", message="Sesión no válida.")
    body = await request.body()
    token = request.headers.get("X-Action-Confirmation")
    if token:
        # Obtain the lock before reading the validation clock: time waiting
        # for another transaction must not extend a proof's lifetime.
        await session.execute(
            text("SELECT id FROM sessions WHERE id=:id FOR UPDATE"), {"id": stored.id}
        )
        stored = await SqlSessionRepository(session).get_by_token_hash(
            hash_session_token(raw_session)
        )
        if stored is None or stored.owner_id != owner.owner_id:
            raise ApiError(status_code=401, code="UNAUTHORIZED", message="Sesión no válida.")
    now = clock.now()
    try:
        stored.require_active(now, SESSION_ABSOLUTE_TTL)
    except (SessionExpiredError, SessionRevokedError) as exc:
        raise ApiError(status_code=401, code="UNAUTHORIZED", message="Sesión no válida.") from exc
    codec = ActionConfirmationCodec(
        request.app.state.container.settings.session_secret.get_secret_value()
    )
    binding = codec.binding(
        session_id=stored.id,
        method=request.method,
        path=request.url.path,
        query=request.url.query,
        body=body,
        action=action_hash,
    )
    if not token:
        token, expires = codec.issue(binding=binding, now=now)
        raise ApiError(
            status_code=428,
            code="CONFIRMATION_REQUIRED",
            message="Revisa y confirma esta acción.",
            details={"confirmation_token": token, "expires_at": expires.isoformat()},
        )
    try:
        nonce, expires_timestamp = codec.verify(token, binding=binding, now=now)
    except ConfirmationError as exc:
        raise ApiError(
            status_code=409, code=str(exc), message="Confirmación no válida o caducada."
        ) from exc
    used = (
        await session.execute(
            text("""WITH active_session AS (
        SELECT id,owner_id FROM sessions WHERE id=:session AND owner_id=:owner
          AND revoked_at IS NULL AND expires_at >= :now AND created_at >= :absolute_anchor
        FOR UPDATE
        ) INSERT INTO owner_action_confirmations
        (nonce,session_id,owner_id,binding_hash,expires_at,consumed_at)
        SELECT :nonce,s.id,s.owner_id,:binding,:expires,:now FROM active_session s
        ON CONFLICT (nonce) DO NOTHING RETURNING nonce"""),
            {
                "nonce": nonce,
                "session": stored.id,
                "owner": owner.owner_id,
                "binding": binding,
                "expires": datetime.fromtimestamp(expires_timestamp, UTC),
                "now": now,
                "absolute_anchor": now - SESSION_ABSOLUTE_TTL,
            },
        )
    ).scalar_one_or_none()
    # Separate durable commit intentionally burns the proof even if the
    # subsequent effect fails. Neither timeout nor rollback authorizes retry.
    await session.commit()
    if used is None:
        raise ApiError(
            status_code=409,
            code="CONFIRMATION_USED",
            message=(
                "Confirmación utilizada o sesión revocada. "
                "Comprueba el estado antes de empezar otra acción."
            ),
        )
    return str(nonce)
