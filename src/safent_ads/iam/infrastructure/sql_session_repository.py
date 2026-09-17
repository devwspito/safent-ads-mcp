"""Adaptador SQL de `SessionRepository` sobre `sessions`
(0001_bootstrap.py, + `origin`/`last_federated_auth_at` de 0052). Solo se
persiste `token_hash`, nunca el token en claro (threat-model.md C-25:
"sesiones con token hasheado").

`origin` se escribe al crear y NO se toca al guardar: el UPDATE nombra
`expires_at` y `revoked_at`, nunca `origin`. Que el origen no se reescriba
jamas no es una regla que haya que recordar -- es que no hay ninguna
sentencia que pueda hacerlo (spec 002b, research.md Decision F).

`save()` YA NO toca `last_federated_auth_at` (T064 security review,
re-verificacion de C-82): lo hacia con `GREATEST(columna, :valor)`, pensado
para que un guardado con un valor mas viejo nunca retrocediera la marca --
pero eso mismo la RESUCITABA. `dependencies.py::current_owner` guarda la
sesion en cada peticion para deslizar `expires_at` (el panel sondea cada
10-60 s); si esa peticion habia leido la sesion ANTES de que
`grants_router.py` invalidara la marca por una revocacion (C-82), su
`save()` posterior volvia a escribir el valor fresco que aun llevaba en
memoria -- `GREATEST(invalidada, fresca) = fresca` (y, para `NULL`,
Postgres `GREATEST` ignora los `NULL`: `GREATEST(NULL, fresca) = fresca`
tambien). `save_federated_mark()` es ahora el UNICO escritor de esa
columna, y solo lo llama `resolve_federated_login.py` -- ningun `save()`
generico (`current_owner`, `logout.py`) puede tocarla ni por accidente."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.iam.domain.session import Session, SessionOrigin

_INSERT_SQL = text(
    """
    INSERT INTO sessions (id, owner_id, token_hash, created_at, expires_at, revoked_at,
                          origin, last_federated_auth_at)
    VALUES (:id, :owner_id, :token_hash, :created_at, :expires_at, :revoked_at,
            :origin, :last_federated_auth_at)
    """
)
_GET_BY_TOKEN_HASH_SQL = text(
    "SELECT id, owner_id, token_hash, created_at, expires_at, revoked_at, "
    "origin, last_federated_auth_at "
    "FROM sessions WHERE token_hash = :token_hash"
)
# El callback de Google no puede leer la cookie de sesion (`SameSite=strict`
# no viaja en una vuelta desde otro sitio, 002b contracts/federated-login.md
# §callback): la atadura es el `session_id` guardado en la transaccion.
_GET_BY_ID_SQL = text(
    "SELECT id, owner_id, token_hash, created_at, expires_at, revoked_at, "
    "origin, last_federated_auth_at "
    "FROM sessions WHERE id = :id"
)
_UPDATE_SQL = text(
    "UPDATE sessions SET expires_at = :expires_at, "
    "revoked_at = COALESCE(revoked_at, :revoked_at) "
    "WHERE id = :id"
)
# Unico escritor de `last_federated_auth_at` (ver docstring del modulo).
# `GREATEST` se conserva aqui -- entre dos escrituras LEGITIMAS (esta misma
# funcion, nunca otra), sigue siendo correcto que la marca solo avance.
_SAVE_FEDERATED_MARK_SQL = text(
    "UPDATE sessions SET last_federated_auth_at = GREATEST(last_federated_auth_at, :at) "
    "WHERE id = :id"
)


def _row_to_session(row: Any) -> Session:  # noqa: ANN401 - fila heterogenea de SQLAlchemy
    return Session(
        session_id=row.id,
        owner_id=row.owner_id,
        token_hash=row.token_hash,
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        origin=SessionOrigin(row.origin),
        last_federated_auth_at=row.last_federated_auth_at,
    )


class SqlSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, session: Session) -> None:
        await self._session.execute(
            _INSERT_SQL,
            {
                "id": str(session.id),
                "owner_id": str(session.owner_id),
                "token_hash": session.token_hash,
                "created_at": session.created_at,
                "expires_at": session.expires_at,
                "revoked_at": session.revoked_at,
                "origin": session.origin.value,
                "last_federated_auth_at": session.last_federated_auth_at,
            },
        )

    async def get_by_token_hash(self, token_hash: str) -> Session | None:
        result = await self._session.execute(_GET_BY_TOKEN_HASH_SQL, {"token_hash": token_hash})
        row = result.one_or_none()
        return None if row is None else _row_to_session(row)

    async def get_by_id(self, session_id: uuid.UUID) -> Session | None:
        result = await self._session.execute(_GET_BY_ID_SQL, {"id": str(session_id)})
        row = result.one_or_none()
        return None if row is None else _row_to_session(row)

    async def save(self, session: Session) -> None:
        await self._session.execute(
            _UPDATE_SQL,
            {
                "id": str(session.id),
                "expires_at": session.expires_at,
                "revoked_at": session.revoked_at,
            },
        )

    async def save_federated_mark(self, session_id: uuid.UUID, at: datetime) -> None:
        await self._session.execute(_SAVE_FEDERATED_MARK_SQL, {"id": str(session_id), "at": at})
