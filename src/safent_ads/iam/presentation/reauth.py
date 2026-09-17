"""`require_reauth`: dependencia compartida de re-autenticacion TOTP
(`X-Reauth-Token`, threat-model.md "TOTP re-auth", security review F2/F3).

Extraida de `composition/execution_rest.py` (donde nacio para
`POST /rules/autonomy-gate/confirmations`) para que
`notifications/presentation/rest.py::POST /telegram/pairing/start`
(rest-api.md: "X-Reauth-Token") la reuse tal cual -- un solo verificador
TOTP, nunca uno segundo. `action_hash` sigue siendo responsabilidad de
cada llamador (ata el codigo a la accion EXACTA que confirma, 0020
`totp_reauth_confirmations`); esta funcion solo sabe quemar el contador
RFC 6238 que hizo match, una vez, para la accion que se le pasa."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.composition.container import Container
from safent_ads.iam.application.session_policy import LOCKOUT_THRESHOLD, LOCKOUT_WINDOW
from safent_ads.iam.infrastructure.aesgcm_totp_cipher import (
    PURPOSE_TOTP_SECRET,
    AesGcmTotpCipher,
)
from safent_ads.iam.infrastructure.pyotp_totp_verifier import PyotpTotpVerifier
from safent_ads.iam.infrastructure.sql_login_attempt_repository import SqlLoginAttemptRepository
from safent_ads.iam.infrastructure.sql_owner_repository import SqlOwnerRepository
from safent_ads.iam.presentation.dependencies import AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.shared.clock import Clock
from safent_ads.shared.net.client_ip import resolve_client_ip

# 0020_totp_replay_guard / threat-model.md TOTP re-auth defecto 1: quema el
# contador RFC 6238 que hizo match. `UNIQUE (owner_id, time_step)` sin
# `action_hash` en la clave a proposito -- si el codigo ya se uso, el
# INSERT choca sea cual sea la accion, asi que un solo codigo nunca
# confirma dos limites distintos (defecto 2). `action_hash` se guarda solo
# para auditoria de a que accion exacta se ato ese codigo.
_BURN_TOTP_TIME_STEP = text("""
    INSERT INTO totp_reauth_confirmations (owner_id, time_step, action_hash, confirmed_at)
    VALUES (:owner_id, :time_step, :action_hash, :confirmed_at)
    ON CONFLICT ON CONSTRAINT totp_reauth_confirmations_unique DO NOTHING
    RETURNING id
""")


def _client_ip(request: Request) -> str | None:
    """Code review 17-sep: delega en `shared/net/client_ip.py` (UN solo
    lugar) -- antes devolvia el literal `"unknown"` sin `request.client`,
    que `login_attempts.ip_address` (`INET`) no puede aceptar. El salto de
    confianza sale del container de la app (`request.app.state.container.
    settings.trusted_proxy_hops`), mismo patron ya usado en este modulo de
    presentacion (`action_confirmation.py`) para no cambiar la firma de
    `require_reauth` ni la de sus llamadores."""
    container: Container = request.app.state.container
    return resolve_client_ip(
        forwarded_for=request.headers.get("x-forwarded-for"),
        peer_ip=request.client.host if request.client is not None else None,
        trusted_proxy_hops=container.settings.trusted_proxy_hops,
    )


async def _reject_if_reauth_locked(
    login_attempts: SqlLoginAttemptRepository, clock: Clock, email: str, ip_address: str | None
) -> None:
    """Reutiliza el bloqueo 5/15 min de `iam` (threat-model.md C-25,
    `login_attempts`/`VerifyTotp._reject_if_locked`) para el mismo
    propietario+IP, en vez de inventar un segundo mecanismo de bloqueo."""
    since = clock.now() - LOCKOUT_WINDOW
    failures = await login_attempts.count_recent_failures(
        email=email, ip_address=ip_address, since=since
    )
    if failures >= LOCKOUT_THRESHOLD:
        raise ApiError(
            status_code=429, code="ACCOUNT_LOCKED", message="Demasiados intentos recientes."
        )


async def _burn_totp_time_step(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    time_step: int,
    action_hash: str,
    at: datetime,
) -> bool:
    """`True` solo si ESTE proceso quemo la fila -- un `time_step` ya
    quemado (misma accion o distinta) choca en el `INSERT` y devuelve
    `False` sin tocar nada (T075/F2-F3 TOTP re-auth defectos 1 y 2)."""
    result = await session.execute(
        _BURN_TOTP_TIME_STEP,
        {
            "owner_id": owner_id,
            "time_step": time_step,
            "action_hash": action_hash,
            "confirmed_at": at,
        },
    )
    return result.mappings().one_or_none() is not None


async def _record_reauth_attempt(
    session: AsyncSession,
    login_attempts: SqlLoginAttemptRepository,
    *,
    email: str,
    ip_address: str | None,
    succeeded: bool,
) -> None:
    """Confirma YA -- no al final del handler. Sin este commit propio, un
    fallo posterior no relacionado haria rollback de la quema del
    codigo/del intento fallido junto con todo lo demas -- el codigo TOTP
    seguiria "vivo" y el contador de bloqueo 5/15 min nunca avanzaria."""
    await login_attempts.record(email=email, succeeded=succeeded, ip_address=ip_address)
    await session.commit()


async def require_reauth(
    request: Request,
    session: AsyncSession,
    owner: AuthenticatedOwner,
    *,
    action_hash: str,
    totp_enc_key: str,
    clock: Clock,
) -> None:
    """Verifica `X-Reauth-Token` y quema el contador RFC 6238 que hizo
    match, atado a `action_hash` (0020_totp_replay_guard). El llamador
    abre/confirma la sesion que le pasa; esta funcion solo anade sus
    propios commits puntuales de intentos (`_record_reauth_attempt`)."""
    token = request.headers.get("X-Reauth-Token")
    if not token:
        raise ApiError(status_code=401, code="REAUTH_REQUIRED", message="Falta X-Reauth-Token.")

    ip_address = _client_ip(request)
    login_attempts = SqlLoginAttemptRepository(session)
    await _reject_if_reauth_locked(login_attempts, clock, owner.email, ip_address)

    stored_owner = await SqlOwnerRepository(session).get_by_id(owner.owner_id)
    if stored_owner is None or stored_owner.totp_secret_encrypted is None:
        raise ApiError(
            status_code=401,
            code="REAUTH_REQUIRED",
            message="TOTP no habilitado para el propietario.",
        )
    cipher = AesGcmTotpCipher(totp_enc_key)
    secret = cipher.decrypt(stored_owner.totp_secret_encrypted, purpose=PURPOSE_TOTP_SECRET)
    now = clock.now()
    # `matched_time_step`, no `verify_code`: hace falta el contador RFC 6238
    # exacto (paso de 30s, +/-1 de tolerancia) para poder quemarlo abajo --
    # sin eso el mismo codigo es replayable durante toda la ventana de
    # tolerancia (threat-model.md TOTP re-auth, defecto 1).
    time_step = PyotpTotpVerifier().matched_time_step(secret, token, at=now)
    if time_step is None:
        await _record_reauth_attempt(
            session, login_attempts, email=owner.email, ip_address=ip_address, succeeded=False
        )
        raise ApiError(status_code=401, code="REAUTH_REQUIRED", message="Codigo TOTP invalido.")

    burned = await _burn_totp_time_step(
        session, owner_id=owner.owner_id, time_step=time_step, action_hash=action_hash, at=now
    )
    if not burned:
        await _record_reauth_attempt(
            session, login_attempts, email=owner.email, ip_address=ip_address, succeeded=False
        )
        raise ApiError(
            status_code=401, code="REAUTH_REQUIRED", message="Codigo TOTP ya utilizado."
        )
    await _record_reauth_attempt(
        session, login_attempts, email=owner.email, ip_address=ip_address, succeeded=True
    )
