"""`current_owner`/`require_business_access` (contracts/rest-api.md,
threat-model.md C-27): "RequireBusinessAccess es dependencia FastAPI en
cada router, no disciplina por handler -- la barrida IDOR de oposads
demostro que la disciplina falla". `require_business_access` depende de
`current_owner` primero: sin sesion valida siempre es 401 antes de que se
llegue a decidir si el negocio existe (404, nunca 403, para no filtrar
existencia)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.composition.container import Container
from safent_ads.iam.application.session_issuance import hash_session_token
from safent_ads.iam.application.session_policy import SESSION_ABSOLUTE_TTL, SESSION_IDLE_TTL
from safent_ads.iam.domain.errors import SessionExpiredError, SessionRevokedError
from safent_ads.iam.domain.session import Session
from safent_ads.iam.infrastructure.sql_business_directory import SqlBusinessDirectory
from safent_ads.iam.infrastructure.sql_owner_repository import SqlOwnerRepository
from safent_ads.iam.infrastructure.sql_session_repository import SqlSessionRepository
from safent_ads.iam.presentation.errors import ApiError

SESSION_COOKIE_NAME = "ads_session"


@dataclass(frozen=True, slots=True)
class AuthenticatedOwner:
    owner_id: uuid.UUID
    email: str


def _unauthorized() -> ApiError:
    return ApiError(status_code=401, code="UNAUTHORIZED", message="Sesion invalida o ausente.")


def _not_found() -> ApiError:
    return ApiError(status_code=404, code="NOT_FOUND", message="No encontrado.")


async def current_owner(request: Request) -> AuthenticatedOwner:
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        raise _unauthorized()

    container: Container = request.app.state.container
    token_hash = hash_session_token(raw_token)

    async with container.session_factory() as db_session:
        sessions = SqlSessionRepository(db_session)
        session = await sessions.get_by_token_hash(token_hash)
        if session is None:
            raise _unauthorized()

        now = container.clock.now()
        try:
            session.require_active(now, SESSION_ABSOLUTE_TTL)
        except (SessionExpiredError, SessionRevokedError) as exc:
            raise _unauthorized() from exc

        # Perf (16-sep, item 2): `touch()` sigue deslizando `expires_at` en
        # memoria en cada peticion (misma semantica de idle/absolute TTL de
        # siempre), pero solo escribimos cuando el movimiento supera
        # `TOUCH_PERSISTENCE_THRESHOLD` -- el panel sondea cada 10-60 s y
        # antes eso era un UPDATE por peticion autenticada.
        should_persist_touch = session.touch(now, SESSION_IDLE_TTL, SESSION_ABSOLUTE_TTL)
        if should_persist_touch:
            await sessions.save(session)

        owner = await SqlOwnerRepository(db_session).get_by_id(session.owner_id)
        if owner is None:
            raise _unauthorized()

        if should_persist_touch:
            await db_session.commit()

    return AuthenticatedOwner(owner_id=owner.id, email=str(owner.email))


CURRENT_OWNER = Depends(current_owner)


def set_session_cookie(response: Response, raw_token: str) -> None:
    """Unico punto que fija la cookie de sesion (spec 002b contracts/
    federated-login.md §callback: "exactamente la misma cookie que emite
    POST /auth/login, con los mismos plazos"). Antes vivia duplicada como
    `_set_session_cookie` en `router.py`; extraerla evita que un atributo
    de seguridad (`HttpOnly`/`Secure`/`SameSite`/`path`) pueda divergir
    entre las dos rutas que emiten sesion."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/api",
        max_age=int(SESSION_ABSOLUTE_TTL.total_seconds()),
    )


async def current_session(request: Request, db_session: AsyncSession) -> Session:
    """Contrapartida aditiva de `current_owner` (002b research.md Decision B
    / tasks.md decision B): resuelve el agregado `Session`, no
    `AuthenticatedOwner` -- para quien necesita `session_id`/`origin`/
    frescura (`fresh_identification.py`, `/auth/me`). Toma un `db_session`
    YA ABIERTO en vez de abrir el suyo propio (a diferencia de
    `current_owner`): quien la llama a media transaccion
    (`require_fresh_identification`) no paga una segunda ida y vuelta.
    Solo lectura: no hace `touch()` -- `current_owner`, que ya corrio antes
    en la misma peticion como dependencia de FastAPI, ya lo hizo.
    `current_owner` en si no se toca."""
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        raise _unauthorized()
    token_hash = hash_session_token(raw_token)
    session = await SqlSessionRepository(db_session).get_by_token_hash(token_hash)
    if session is None:
        raise _unauthorized()
    container: Container = request.app.state.container
    try:
        session.require_active(container.clock.now(), SESSION_ABSOLUTE_TTL)
    except (SessionExpiredError, SessionRevokedError) as exc:
        raise _unauthorized() from exc
    return session


async def require_business_access(
    business_id: uuid.UUID,
    request: Request,
    _owner: AuthenticatedOwner = CURRENT_OWNER,
) -> uuid.UUID:
    container: Container = request.app.state.container
    async with container.session_factory() as db_session:
        directory = SqlBusinessDirectory(db_session)
        exists = await directory.exists(business_id)

    if not exists:
        raise _not_found()
    return business_id
