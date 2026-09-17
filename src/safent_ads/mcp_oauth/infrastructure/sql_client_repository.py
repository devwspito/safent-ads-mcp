"""`SqlClientRepository` sobre `oauth_clients` (0035_mcp_oauth, tasks.md
T006). REGISTERED/TRUSTED (data-model.md) no tiene columna propia: se
deriva de `last_seen_at` (`OAuthClient.mark_trusted()` siempre lo fija
junto al estado, `presentation/sdk_provider.py`) -- una columna redundante
solo podria desincronizarse de la que ya existe.

Lectura tolerante, escritura y autorizacion estrictas (revision de
codigo, 17-sep, D-11): `get_many` alimenta la lista del panel
(`ListGrants`), y una fila antigua puede ya no cumplir una regla que el
dominio endurecio despues -- un destino remoto (D-11) o un `client_name`
con caracteres bidireccionales (C-70 pieza 3). Si esa fila tumbara la
consulta, el dueno perderia la lista ENTERA de aplicaciones con acceso --
incluido el boton «Quitar acceso» de la peligrosa, que es justo la que
querria cortar. Se omite esa fila del diccionario (con aviso) y
`ListGrants` la pinta por su `client_id`, revocable. Por eso se atrapa
`DomainError` entero y no una excepcion concreta: la lista no puede
romperse por la SIGUIENTE regla que alguien endurezca. `get_by_id` NO
afloja: autorizar y consentir fallan cerrado."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.mcp_oauth.domain.client import (
    OAuthClient,
    OAuthClientState,
    RedirectUri,
    TokenEndpointAuthMethod,
)
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.shared.errors import DomainError

logger = structlog.get_logger(__name__)

_UPSERT_SQL = text("""
    INSERT INTO oauth_clients
        (client_id, client_name, redirect_uris, token_endpoint_auth_method,
         client_secret_hash, grant_types, requested_scopes, created_at, last_seen_at)
    VALUES
        (:client_id, :client_name, CAST(:redirect_uris AS jsonb), :token_endpoint_auth_method,
         :client_secret_hash, CAST(:grant_types AS jsonb), :requested_scopes, :created_at,
         :last_seen_at)
    ON CONFLICT (client_id) DO UPDATE SET
        client_name = EXCLUDED.client_name,
        redirect_uris = EXCLUDED.redirect_uris,
        token_endpoint_auth_method = EXCLUDED.token_endpoint_auth_method,
        client_secret_hash = EXCLUDED.client_secret_hash,
        grant_types = EXCLUDED.grant_types,
        requested_scopes = EXCLUDED.requested_scopes,
        last_seen_at = EXCLUDED.last_seen_at
""")

_SELECT_COLUMNS = """
    SELECT client_id, client_name, redirect_uris, token_endpoint_auth_method,
           client_secret_hash, grant_types, requested_scopes, created_at, last_seen_at
      FROM oauth_clients
"""
_SELECT_BY_ID_SQL = text(f"{_SELECT_COLUMNS} WHERE client_id = :client_id")  # noqa: S608
# I7: una sola consulta para varios `client_id` (`ListGrants`, 2N+1 -> O(1)).
_SELECT_MANY_SQL = text(f"{_SELECT_COLUMNS} WHERE client_id = ANY(:client_ids)")  # noqa: S608

_COUNT_UNCONSENTED_SQL = text("SELECT count(*) FROM oauth_clients WHERE last_seen_at IS NULL")

# M3 (threat-model.md C-42): la subconsulta ordena por `created_at` para
# encontrar el REGISTERED mas antiguo; el `DELETE ... WHERE client_id = (...)`
# de fuera lo borra en una unica sentencia atomica, sin ida y vuelta
# SELECT-luego-DELETE que pudiera desalojar el candidato equivocado bajo
# concurrencia.
#
# Nit de la revision de seguridad final (16-sep): `created_at < :cutoff`
# excluye a cualquier REGISTERED lo bastante joven para tener una
# `AuthorizationRequest` PENDING en curso (vive hasta
# `AUTHORIZATION_REQUEST_TTL`, `application/policy.py`) -- desalojarlo a
# medio registro tumbaria esa solicitud via CASCADE. Si ningun candidato
# es lo bastante viejo, el `DELETE` no afecta ninguna fila (no-op): el
# tope duro (`MAX_UNCONSENTED_CLIENTS_HARD_CEILING`) sigue siendo el freno
# real si el desalojo no puede seguir el ritmo.
_EVICT_OLDEST_UNCONSENTED_SQL = text("""
    DELETE FROM oauth_clients
     WHERE client_id = (
         SELECT client_id
           FROM oauth_clients
          WHERE last_seen_at IS NULL AND created_at < :cutoff
          ORDER BY created_at ASC
          LIMIT 1
     )
""")


def _row_to_client(row: Any) -> OAuthClient:  # noqa: ANN401 - fila heterogenea de SQLAlchemy
    is_trusted = row.last_seen_at is not None
    return OAuthClient(
        client_id=row.client_id,
        client_name=row.client_name,
        redirect_uris=tuple(RedirectUri(uri) for uri in row.redirect_uris),
        token_endpoint_auth_method=TokenEndpointAuthMethod(row.token_endpoint_auth_method),
        client_secret_hash=row.client_secret_hash,
        grant_types=tuple(row.grant_types),
        requested_scope=ScopeSet.parse(row.requested_scopes),
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
        state=OAuthClientState.TRUSTED if is_trusted else OAuthClientState.REGISTERED,
    )


def _hydrate_for_listing(row: Any) -> OAuthClient | None:  # noqa: ANN401 - fila heterogenea
    """`None` en vez de excepcion cuando la fila ya no cumple las reglas
    del dominio (destino remoto de D-11, `client_name` con caracteres
    bidireccionales de C-70(3), o la que venga). Solo para LEER la lista
    del panel: omitir una fila deja al dueno con una entrada menos de
    detalle; dejar escapar el error le deja sin lista y sin poder revocar
    nada. Solo el tipo del error entra en el registro: el mensaje lleva el
    valor rechazado, que es entrada de un tercero."""
    try:
        return _row_to_client(row)
    except DomainError as exc:
        logger.warning(
            "mcp_oauth_client_omitted_from_listing",
            client_id=row.client_id,
            error_type=type(exc).__name__,
        )
        return None


class SqlClientRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, client_id: str) -> OAuthClient | None:
        row = (
            await self._session.execute(_SELECT_BY_ID_SQL, {"client_id": client_id})
        ).one_or_none()
        return None if row is None else _row_to_client(row)

    async def get_many(self, client_ids: Iterable[str]) -> dict[str, OAuthClient]:
        ids = list(client_ids)
        if not ids:
            return {}
        rows = (await self._session.execute(_SELECT_MANY_SQL, {"client_ids": ids})).all()
        hydrated = ((row.client_id, _hydrate_for_listing(row)) for row in rows)
        return {client_id: client for client_id, client in hydrated if client is not None}

    async def save(self, client: OAuthClient) -> None:
        await self._session.execute(_UPSERT_SQL, _client_params(client))

    async def count_unconsented(self) -> int:
        result = await self._session.execute(_COUNT_UNCONSENTED_SQL)
        return int(result.scalar_one())

    async def evict_oldest_unconsented(self, *, cutoff: datetime) -> None:
        await self._session.execute(_EVICT_OLDEST_UNCONSENTED_SQL, {"cutoff": cutoff})


def _client_params(client: OAuthClient) -> dict[str, Any]:
    return {
        "client_id": client.id,
        "client_name": client.client_name,
        "redirect_uris": json.dumps([str(uri) for uri in client.redirect_uris]),
        "token_endpoint_auth_method": client.token_endpoint_auth_method.value,
        "client_secret_hash": client.client_secret_hash,
        "grant_types": json.dumps(list(client.grant_types)),
        "requested_scopes": str(client.requested_scope),
        "created_at": client.created_at,
        "last_seen_at": client.last_seen_at,
    }
