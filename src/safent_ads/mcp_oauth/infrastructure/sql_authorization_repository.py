"""`SqlAuthorizationRequestRepository` sobre `oauth_authorization_requests`
(0035_mcp_oauth, tasks.md T006, threat-model.md C-38).

El canje es atomico por diseno de la base, no del proceso (plan.md
"Transacciones"): `save()` de una solicitud en REDEEMED emite
`UPDATE ... WHERE state = 'CONSENTED' AND expires_at > :now RETURNING
txn_id`; si no afecta ninguna fila puede ser por dos motivos MUY
distintos, que M5 de la revision de seguridad (16-sep) deja de confundir:
otra transaccion ya la marco REDEEMED/DENIED/CONSENTED primero
(`CodeAlreadyRedeemedError`/`AuthorizationRequestNotPendingError`, el
dominio ya lanza lo mismo para el caso de un solo proceso), o la fila
seguia en el estado esperado pero su plazo ya paso
(`AuthorizationRequestExpiredError`, C-38: un codigo/solicitud caducada no
es lo mismo que uno ya canjeado -- una fila REDEEMED de verdad SI debe
levantar `CodeAlreadyRedeemedError`).

`expires_at` es una sola columna para dos plazos distintos por diseno de
0035 (`ix_oauth_authorization_requests_live`, C-58): mientras PENDING es el
plazo de los 10 min; en cuanto se consiente pasa a ser el plazo del codigo
de 60 s (`AuthorizationRequest.code_expires_at`) -- el valor original de
PENDING deja de leerse en cuanto el estado ya no es PENDING
(`AuthorizationRequest.is_expired`), asi que sobrescribirlo no pierde
ninguna comprobacion viva."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.mcp_oauth.domain.authorization import (
    AuthorizationRequest,
    AuthorizationRequestState,
)
from safent_ads.mcp_oauth.domain.errors import (
    AuthorizationRequestExpiredError,
    AuthorizationRequestNotPendingError,
    CodeAlreadyRedeemedError,
)
from safent_ads.mcp_oauth.domain.grant import TokenHash
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import DomainError

# `redirect_uri_explicit` (RFC 8252 §7.3, `sdk:handlers/token.py:166-183`):
# nuestros clientes (Claude Code, Codex) siempre lo mandan explicito en
# `/authorize` (contracts/oauth.md §4 "Query: ... redirect_uri ..."); el
# dominio (T004/T005) no modela la rama "omitido, cae al unico registrado"
# porque ningun caso de uso propio la necesita -- solo la lee el SDK al
# comparar con `/token`. Fijarlo a `true` reproduce exactamente ese unico
# camino que este AS emite.
_REDIRECT_URI_EXPLICIT = True

_INSERT_SQL = text("""
    INSERT INTO oauth_authorization_requests
        (txn_id, client_id, owner_id, redirect_uri, redirect_uri_explicit, code_challenge,
         client_state, scopes, resource, code_hash, state, created_at, expires_at,
         consented_at, redeemed_at)
    VALUES
        (:txn_id, :client_id, NULL, :redirect_uri, :redirect_uri_explicit, :code_challenge,
         :client_state, :scopes, :resource, NULL, 'PENDING', :created_at, :expires_at,
         NULL, NULL)
""")

_SELECT_COLUMNS = """
    SELECT txn_id, client_id, owner_id, redirect_uri, code_challenge, client_state,
           scopes, resource, code_hash, state, created_at, expires_at, consented_at, redeemed_at
      FROM oauth_authorization_requests
"""
_SELECT_BY_ID_SQL = text(f"{_SELECT_COLUMNS} WHERE txn_id = :txn_id")
_SELECT_BY_CODE_HASH_SQL = text(f"{_SELECT_COLUMNS} WHERE code_hash = :code_hash")

_COUNT_PENDING_SQL = text(
    "SELECT count(*) FROM oauth_authorization_requests"
    " WHERE client_id = :client_id AND state = 'PENDING'"
)

_UPDATE_TO_CONSENTED_SQL = text("""
    UPDATE oauth_authorization_requests
       SET state = 'CONSENTED', owner_id = :owner_id, code_hash = :code_hash,
           consented_at = :consented_at, expires_at = :expires_at
     WHERE txn_id = :txn_id AND state = 'PENDING' AND expires_at > :now
    RETURNING txn_id
""")

_UPDATE_TO_DENIED_SQL = text("""
    UPDATE oauth_authorization_requests SET state = 'DENIED'
     WHERE txn_id = :txn_id AND state = 'PENDING' AND expires_at > :now
    RETURNING txn_id
""")

_UPDATE_TO_REDEEMED_SQL = text("""
    UPDATE oauth_authorization_requests SET state = 'REDEEMED', redeemed_at = :redeemed_at
     WHERE txn_id = :txn_id AND state = 'CONSENTED' AND expires_at > :now
    RETURNING txn_id
""")

# Nit de la revision de seguridad final (16-sep): `AND state IN ('PENDING',
# 'CONSENTED')` para que una transicion a EXPIRED nunca pueda sobrescribir
# una fila ya REDEEMED (ni ninguna otra terminal) -- sin este filtro, un
# `save()` con `request.state == EXPIRED` construido sobre datos obsoletos
# borraria en silencio el `code_hash`/estado de un canje ya consumado.
_UPDATE_TO_EXPIRED_SQL = text("""
    UPDATE oauth_authorization_requests SET state = 'EXPIRED', code_hash = NULL
     WHERE txn_id = :txn_id AND state IN ('PENDING', 'CONSENTED')
    RETURNING txn_id
""")

# M5: tras un UPDATE de 0 filas, distingue "la fila seguia en el estado de
# partida pero ya caduco" (AuthorizationRequestExpiredError) de "la fila ya
# transiciono a otra cosa" (los errores de siempre) -- sin esta consulta,
# ambos casos colapsaban en el mismo `CodeAlreadyRedeemedError`/
# `AuthorizationRequestNotPendingError` segun el estado DESTINO intentado,
# nunca el estado y plazo REALES de la fila.
_SELECT_STATE_AND_EXPIRY_SQL = text(
    "SELECT state, expires_at FROM oauth_authorization_requests WHERE txn_id = :txn_id"
)

# Estado de partida que cada transicion exige (mismo `WHERE state = ...`
# de arriba): si la fila SIGUE ahi pero ya paso `expires_at`, la causa es
# la expiracion, no un replay ni un estado incompatible.
_EXPECTED_SOURCE_STATE = {
    AuthorizationRequestState.CONSENTED: "PENDING",
    AuthorizationRequestState.DENIED: "PENDING",
    AuthorizationRequestState.REDEEMED: "CONSENTED",
}


def _row_to_request(row: Any) -> AuthorizationRequest:  # noqa: ANN401 - fila heterogenea
    code_hash = TokenHash(row.code_hash) if row.code_hash is not None else None
    return AuthorizationRequest(
        txn_id=row.txn_id,
        client_id=row.client_id,
        redirect_uri=row.redirect_uri,
        code_challenge=row.code_challenge,
        client_state=row.client_state,
        scope_set=ScopeSet.parse(row.scopes),
        resource=ResourceIndicator(row.resource),
        created_at=row.created_at,
        expires_at=row.expires_at,
        state=AuthorizationRequestState(row.state),
        code_hash=code_hash,
        owner_id=row.owner_id,
        consented_at=row.consented_at,
        redeemed_at=row.redeemed_at,
        # `expires_at` sirve a las dos fases (ver docstring del modulo): una
        # vez hay codigo, tambien es su plazo.
        code_expires_at=row.expires_at if code_hash is not None else None,
    )


class SqlAuthorizationRequestRepository:
    def __init__(self, session: AsyncSession, *, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    async def create(self, request: AuthorizationRequest) -> None:
        await self._session.execute(
            _INSERT_SQL,
            {
                "txn_id": str(request.id),
                "client_id": request.client_id,
                "redirect_uri": request.redirect_uri,
                "redirect_uri_explicit": _REDIRECT_URI_EXPLICIT,
                "code_challenge": request.code_challenge,
                "client_state": request.client_state,
                "scopes": str(request.scope_set),
                "resource": request.resource.value,
                "created_at": request.created_at,
                "expires_at": request.expires_at,
            },
        )

    async def save(self, request: AuthorizationRequest) -> None:
        now = self._clock.now()
        transitioned = await self._transition(request, now)
        if not transitioned:
            raise await self._conflict_error(request, now)

    async def get_by_id(self, txn_id: uuid.UUID) -> AuthorizationRequest | None:
        result = await self._session.execute(_SELECT_BY_ID_SQL, {"txn_id": str(txn_id)})
        row = result.one_or_none()
        return None if row is None else _row_to_request(row)

    async def get_by_code_hash(self, code_hash: TokenHash) -> AuthorizationRequest | None:
        row = (
            await self._session.execute(_SELECT_BY_CODE_HASH_SQL, {"code_hash": str(code_hash)})
        ).one_or_none()
        return None if row is None else _row_to_request(row)

    async def count_pending_for_client(self, client_id: str) -> int:
        result = await self._session.execute(_COUNT_PENDING_SQL, {"client_id": client_id})
        return int(result.scalar_one())

    async def _transition(self, request: AuthorizationRequest, now: datetime) -> bool:
        state = request.state
        if state is AuthorizationRequestState.CONSENTED:
            return await self._consent(request, now)
        if state is AuthorizationRequestState.REDEEMED:
            return await self._redeem(request, now)
        if state is AuthorizationRequestState.DENIED:
            return await self._deny(request, now)
        if state is AuthorizationRequestState.EXPIRED:
            return await self._expire(request)
        raise AuthorizationRequestNotPendingError(
            f"save() no admite persistir el estado {state} directamente"
        )

    async def _consent(self, request: AuthorizationRequest, now: datetime) -> bool:
        assert request.owner_id is not None  # noqa: S101 - invariante de dominio: CONSENTED lo fija
        assert request.code_hash is not None  # noqa: S101 - idem
        assert request.code_expires_at is not None  # noqa: S101 - idem
        result = await self._session.execute(
            _UPDATE_TO_CONSENTED_SQL,
            {
                "txn_id": str(request.id),
                "owner_id": str(request.owner_id),
                "code_hash": str(request.code_hash),
                "consented_at": request.consented_at,
                "expires_at": request.code_expires_at,
                "now": now,
            },
        )
        return result.mappings().one_or_none() is not None

    async def _redeem(self, request: AuthorizationRequest, now: datetime) -> bool:
        result = await self._session.execute(
            _UPDATE_TO_REDEEMED_SQL,
            {"txn_id": str(request.id), "redeemed_at": request.redeemed_at, "now": now},
        )
        return result.mappings().one_or_none() is not None

    async def _deny(self, request: AuthorizationRequest, now: datetime) -> bool:
        result = await self._session.execute(
            _UPDATE_TO_DENIED_SQL, {"txn_id": str(request.id), "now": now}
        )
        return result.mappings().one_or_none() is not None

    async def _expire(self, request: AuthorizationRequest) -> bool:
        result = await self._session.execute(_UPDATE_TO_EXPIRED_SQL, {"txn_id": str(request.id)})
        return result.mappings().one_or_none() is not None

    async def _conflict_error(self, request: AuthorizationRequest, now: datetime) -> DomainError:
        """M5: distingue expiracion de conflicto de estado real -- ver
        docstring del modulo. `state`/`expires_at` son los de la fila TAL
        CUAL esta en este instante, no los que `request` intentaba fijar."""
        row = (
            await self._session.execute(
                _SELECT_STATE_AND_EXPIRY_SQL, {"txn_id": str(request.id)}
            )
        ).one_or_none()
        target_state = request.state
        if row is not None and self._is_expired_source_row(row, target_state, now):
            return AuthorizationRequestExpiredError(
                "la solicitud (o su codigo) supero su plazo antes de transicionar"
            )
        if target_state is AuthorizationRequestState.REDEEMED:
            return CodeAlreadyRedeemedError("el codigo ya fue canjeado por otra transaccion")
        return AuthorizationRequestNotPendingError(
            f"la solicitud ya no esta PENDING al intentar transicionar a {target_state}"
        )

    @staticmethod
    def _is_expired_source_row(
        row: Any, target_state: AuthorizationRequestState, now: datetime  # noqa: ANN401
    ) -> bool:
        expected_source = _EXPECTED_SOURCE_STATE.get(target_state)
        return bool(row.state == expected_source and row.expires_at <= now)
