"""`ListGrants` (enmienda T015b, threat-model.md C-55): agentes conectados
para el panel «Agentes conectados».

`last_used_at` (contracts/oauth.md §9, data-model.md "Desviaciones
aplicadas en 0035" #9) es `max(issued_at)` de los access tokens de la
concesion -- `oauth_tokens` no lleva su propia columna `last_used_at` a
proposito (evita una escritura por llamada MCP). Cada rotacion (T006)
anade un access token nuevo con su propio `issued_at`, asi que el maximo
siempre refleja el ultimo canje o refresco, sin depender de si el cliente
sigue existiendo."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from safent_ads.mcp_oauth.application.ports import ClientRepository, GrantRepository
from safent_ads.mcp_oauth.domain.client import OAuthClient
from safent_ads.mcp_oauth.domain.grant import Grant, TokenKind
from safent_ads.mcp_oauth.domain.scope import ScopeSet


@dataclass(frozen=True, slots=True, kw_only=True)
class ConnectedGrant:
    grant_id: uuid.UUID
    client_id: str
    client_name: str
    redirect_host: str
    scopes: ScopeSet
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None


class ListGrants:
    def __init__(self, *, grants: GrantRepository, clients: ClientRepository) -> None:
        self._grants = grants
        self._clients = clients

    async def execute(self, *, owner_id: uuid.UUID) -> list[ConnectedGrant]:
        grants = await self._grants.list_active_for_owner(owner_id)
        # I7 (revision de seguridad, 16-sep): una sola consulta para todos
        # los clientes de las concesiones listadas, en vez de una por
        # concesion (2N+1 -> 2 consultas totales, la otra mitad ya la
        # resolvio `list_active_for_owner`).
        clients_by_id = await self._clients.get_many(
            {grant.client_id for grant in grants}
        )
        return [self._to_summary(grant, clients_by_id.get(grant.client_id)) for grant in grants]

    @classmethod
    def _to_summary(cls, grant: Grant, client: OAuthClient | None) -> ConnectedGrant:
        refresh_token = grant.active_token(TokenKind.REFRESH)
        return ConnectedGrant(
            grant_id=grant.id,
            client_id=grant.client_id,
            client_name=client.client_name if client is not None else grant.client_id,
            redirect_host=cls._redirect_host(client),
            scopes=grant.scope_set,
            created_at=grant.created_at,
            expires_at=refresh_token.expires_at if refresh_token is not None else None,
            last_used_at=cls._last_used_at(grant),
        )

    @staticmethod
    def _redirect_host(client: OAuthClient | None) -> str:
        if client is None or not client.redirect_uris:
            return ""
        return urlsplit(client.redirect_uris[0].value).netloc

    @staticmethod
    def _last_used_at(grant: Grant) -> datetime | None:
        access_issued_at = [t.issued_at for t in grant.tokens if t.kind is TokenKind.ACCESS]
        return max(access_issued_at) if access_issued_at else None
