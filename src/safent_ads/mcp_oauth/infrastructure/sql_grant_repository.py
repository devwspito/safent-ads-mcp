"""`SqlGrantRepository` sobre `oauth_grants`/`oauth_tokens` (0035_mcp_oauth,
tasks.md T006, threat-model.md C-43).

`Grant.authorization_request_id` (dominio) mapea a la columna
`authorization_txn_id` (data-model.md "Desviaciones aplicadas en 0035" #3):
nombres distintos a proposito, el dominio habla de "la solicitud que lo
origino", la columna de "el txn al que esta atado".

Rotacion (data-model.md #4): `ix_oauth_tokens_grant_active` es UNIQUE
parcial (`state = 'ACTIVE'`) -- como mucho un access y un refresh activos
por concesion. `save()` recorre `grant.tokens` en su orden natural (los
tokens rotados, ya marcados ROTATED en memoria, aparecen antes que los
nuevos ACTIVE que los sustituyen: `Grant.rotate_refresh_token` los anexa al
final) y hace un UPSERT por token -- procesar la lista en ese orden, dentro
de la misma transaccion, es lo que evita chocar con el indice: el UPDATE a
ROTATED libera el hueco antes de que el INSERT del sucesor lo ocupe.

`get_by_token_hash_for_rotation` (fix/refresh-rotation-race, C-43): dos
refrescos concurrentes del MISMO token leian el grant con el refresh
ACTIVE cada uno por su cuenta (`get_by_token_hash` normal, sin lock) --
si la transaccion perdedora tardaba lo bastante en arrancar su propia
lectura como para que la ganadora YA hubiese confirmado, esa lectura
tardia encontraba el refresh presentado ya ROTATED y
`Grant.rotate_refresh_token()` lo trataba como un reuso genuino: revocaba
la concesion ENTERA, incluidos los tokens recien emitidos de la
ganadora (`RefreshGrant.execute()`, `domain/grant.py::revoke()`) --
`0 == 1` intermitente en `active_refresh_tokens`. Esta version reserva la
fila del refresh (`FOR UPDATE NOWAIT`) ANTES de leerla: si otra
transaccion ya la tiene reservada ahora mismo, Postgres lo dice al
instante (55P03, `lock_not_available`) y esta llamada se rinde ahi mismo
con `ConcurrentRefreshInProgressError` -- SIN haber leido nada que pueda
confundirse con un reuso asentado, SIN revocar. Un refresh YA ROTATED
que se lee con el lock libre (nadie compitiendo AHORA MISMO) sigue siendo
reuso genuino y revoca igual que siempre: solo cambia el caso de la
carrera legitima."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import TextClause, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.mcp_oauth.application.errors import ConcurrentRefreshInProgressError
from safent_ads.mcp_oauth.domain.grant import Grant, IssuedToken, TokenHash, TokenKind, TokenState
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet

# Postgres 55P03 (`lock_not_available`): lo que devuelve `FOR UPDATE
# NOWAIT` cuando OTRA transaccion ya tiene la fila reservada ahora mismo.
_LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"

_KIND_TO_COLUMN = {TokenKind.ACCESS: "access", TokenKind.REFRESH: "refresh"}
_COLUMN_TO_KIND = {value: key for key, value in _KIND_TO_COLUMN.items()}

_INSERT_GRANT_SQL = text("""
    INSERT INTO oauth_grants
        (grant_id, owner_id, client_id, authorization_txn_id, scopes, resource,
         created_at, revoked_at, revoked_reason)
    VALUES
        (:grant_id, :owner_id, :client_id, :authorization_txn_id, :scopes, :resource,
         :created_at, :revoked_at, :revoked_reason)
""")

_UPDATE_GRANT_REVOCATION_SQL = text("""
    UPDATE oauth_grants SET revoked_at = :revoked_at, revoked_reason = :revoked_reason
     WHERE grant_id = :grant_id
""")

_UPSERT_TOKEN_SQL = text("""
    INSERT INTO oauth_tokens
        (token_hash, grant_id, kind, state, issued_at, expires_at, rotated_from)
    VALUES (:token_hash, :grant_id, :kind, :state, :issued_at, :expires_at, :rotated_from)
    ON CONFLICT (token_hash) DO UPDATE SET state = EXCLUDED.state
""")

_SELECT_GRANT_COLUMNS = """
    SELECT grant_id, owner_id, client_id, authorization_txn_id, scopes, resource,
           created_at, revoked_at, revoked_reason
      FROM oauth_grants
"""
_SELECT_GRANT_BY_ID_SQL = text(f"{_SELECT_GRANT_COLUMNS} WHERE grant_id = :grant_id")  # noqa: S608
_SELECT_GRANT_BY_AUTHORIZATION_TXN_SQL = text(  # noqa: S608
    f"{_SELECT_GRANT_COLUMNS} WHERE authorization_txn_id = :authorization_txn_id"
)
_SELECT_GRANT_BY_TOKEN_HASH_SQL = text(f"""
    {_SELECT_GRANT_COLUMNS}
     WHERE grant_id = (SELECT grant_id FROM oauth_tokens WHERE token_hash = :token_hash)
""")  # noqa: S608
# fix/refresh-rotation-race: reserva la fila del refresh presentado ANTES
# de leer el grant -- `get_by_token_hash_for_rotation` mas abajo. `NOWAIT`
# para no bloquear nunca (sin espera no hay deadlock posible): si la fila
# ya esta reservada por otra rotacion, Postgres lo dice al instante
# (55P03) en vez de dejarnos esperar a que termine y releer un estado que
# ya no es el que vimos al empezar.
_LOCK_TOKEN_FOR_ROTATION_SQL = text("""
    SELECT token_hash FROM oauth_tokens WHERE token_hash = :token_hash FOR UPDATE NOWAIT
""")
_SELECT_ACTIVE_GRANTS_FOR_OWNER_SQL = text(f"""
    {_SELECT_GRANT_COLUMNS}
     WHERE owner_id = :owner_id AND revoked_at IS NULL
     ORDER BY created_at DESC
""")  # noqa: S608

_SELECT_TOKENS_BY_GRANT_SQL = text("""
    SELECT token_hash, kind, state, issued_at, expires_at, rotated_from
      FROM oauth_tokens
     WHERE grant_id = :grant_id
     ORDER BY issued_at
""")

# I7 de la revision de seguridad (16-sep): una sola consulta para los
# tokens de TODOS los `grant_id` pedidos, en vez de una por concesion
# (`_load_tokens`) -- `list_active_for_owner` la usa y agrupa en memoria
# por `grant_id` (`_group_tokens_by_grant`), preservando el orden por
# `issued_at` con el propio `ORDER BY`.
_SELECT_TOKENS_BY_GRANTS_SQL = text("""
    SELECT grant_id, token_hash, kind, state, issued_at, expires_at, rotated_from
      FROM oauth_tokens
     WHERE grant_id = ANY(:grant_ids)
     ORDER BY grant_id, issued_at
""")


def _token_to_row(grant_id: uuid.UUID, token: IssuedToken) -> dict[str, Any]:
    return {
        "token_hash": str(token.token_hash),
        "grant_id": str(grant_id),
        "kind": _KIND_TO_COLUMN[token.kind],
        "state": token.state.value,
        "issued_at": token.issued_at,
        "expires_at": token.expires_at,
        "rotated_from": str(token.rotated_from) if token.rotated_from is not None else None,
    }


def _row_to_token(row: Any) -> IssuedToken:  # noqa: ANN401 - fila heterogenea de SQLAlchemy
    return IssuedToken(
        token_hash=TokenHash(row.token_hash),
        kind=_COLUMN_TO_KIND[row.kind],
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        state=TokenState(row.state),
        rotated_from=TokenHash(row.rotated_from) if row.rotated_from is not None else None,
    )


def _row_to_grant(row: Any, tokens: tuple[IssuedToken, ...]) -> Grant:  # noqa: ANN401
    return Grant(
        grant_id=row.grant_id,
        authorization_request_id=row.authorization_txn_id,
        owner_id=row.owner_id,
        client_id=row.client_id,
        scope_set=ScopeSet.parse(row.scopes),
        resource=ResourceIndicator(row.resource),
        created_at=row.created_at,
        revoked_at=row.revoked_at,
        revoked_reason=row.revoked_reason,
        tokens=tokens,
    )


class SqlGrantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, grant: Grant) -> None:
        await self._session.execute(
            _INSERT_GRANT_SQL,
            {
                "grant_id": str(grant.id),
                "owner_id": str(grant.owner_id),
                "client_id": grant.client_id,
                "authorization_txn_id": str(grant.authorization_request_id),
                "scopes": str(grant.scope_set),
                "resource": grant.resource.value,
                "created_at": grant.created_at,
                "revoked_at": grant.revoked_at,
                "revoked_reason": grant.revoked_reason,
            },
        )
        await self._save_tokens(grant)

    async def save(self, grant: Grant) -> None:
        await self._session.execute(
            _UPDATE_GRANT_REVOCATION_SQL,
            {
                "grant_id": str(grant.id),
                "revoked_at": grant.revoked_at,
                "revoked_reason": grant.revoked_reason,
            },
        )
        await self._save_tokens(grant)

    async def get_by_id(self, grant_id: uuid.UUID) -> Grant | None:
        return await self._load_one(_SELECT_GRANT_BY_ID_SQL, {"grant_id": str(grant_id)})

    async def get_by_token_hash(self, token_hash: TokenHash) -> Grant | None:
        return await self._load_one(
            _SELECT_GRANT_BY_TOKEN_HASH_SQL, {"token_hash": str(token_hash)}
        )

    async def get_by_token_hash_for_rotation(self, token_hash: TokenHash) -> Grant | None:
        try:
            await self._session.execute(
                _LOCK_TOKEN_FOR_ROTATION_SQL, {"token_hash": str(token_hash)}
            )
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) != _LOCK_NOT_AVAILABLE_SQLSTATE:
                raise
            raise ConcurrentRefreshInProgressError(
                "otro refresco esta rotando este token ahora mismo"
            ) from exc
        return await self.get_by_token_hash(token_hash)

    async def get_by_authorization_request_id(self, txn_id: uuid.UUID) -> Grant | None:
        return await self._load_one(
            _SELECT_GRANT_BY_AUTHORIZATION_TXN_SQL, {"authorization_txn_id": str(txn_id)}
        )

    async def list_active_for_owner(self, owner_id: uuid.UUID) -> list[Grant]:
        params = {"owner_id": str(owner_id)}
        rows = (await self._session.execute(_SELECT_ACTIVE_GRANTS_FOR_OWNER_SQL, params)).all()
        tokens_by_grant = await self._load_tokens_for_many(row.grant_id for row in rows)
        return [
            _row_to_grant(row, tokens_by_grant.get(row.grant_id, ())) for row in rows
        ]

    async def _save_tokens(self, grant: Grant) -> None:
        for token in grant.tokens:
            await self._session.execute(_UPSERT_TOKEN_SQL, _token_to_row(grant.id, token))

    async def _load_one(self, statement: TextClause, params: dict[str, Any]) -> Grant | None:
        row = (await self._session.execute(statement, params)).one_or_none()
        if row is None:
            return None
        return _row_to_grant(row, await self._load_tokens(row.grant_id))

    async def _load_tokens(self, grant_id: uuid.UUID) -> tuple[IssuedToken, ...]:
        rows = (
            await self._session.execute(_SELECT_TOKENS_BY_GRANT_SQL, {"grant_id": str(grant_id)})
        ).all()
        return tuple(_row_to_token(row) for row in rows)

    async def _load_tokens_for_many(
        self, grant_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[IssuedToken, ...]]:
        ids = [str(grant_id) for grant_id in grant_ids]
        if not ids:
            return {}
        rows = (
            await self._session.execute(_SELECT_TOKENS_BY_GRANTS_SQL, {"grant_ids": ids})
        ).all()
        by_grant: dict[uuid.UUID, list[IssuedToken]] = {}
        for row in rows:
            by_grant.setdefault(row.grant_id, []).append(_row_to_token(row))
        return {grant_id: tuple(tokens) for grant_id, tokens in by_grant.items()}
