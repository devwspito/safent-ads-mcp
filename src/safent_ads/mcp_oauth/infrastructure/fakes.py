"""Dobles en memoria de los repositorios de `mcp_oauth`, para que los
tests de casos de uso corran sin Postgres (mismo patron que
`iam/infrastructure/in_memory_*.py`, tasks.md T005). El hasheo y la
generacion de tokens no se fakean: `Sha256TokenHasher`/
`SecretsOpaqueTokenFactory` (T007) ya son stdlib puro y baratos -- fakearlos
solo añadiria una segunda implementacion que mantener."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime

from safent_ads.mcp_oauth.domain.authorization import (
    AuthorizationRequest,
    AuthorizationRequestState,
)
from safent_ads.mcp_oauth.domain.client import OAuthClient, OAuthClientState
from safent_ads.mcp_oauth.domain.grant import Grant, TokenHash


class InMemoryClientRepository:
    def __init__(self, clients: list[OAuthClient] | None = None) -> None:
        self._clients: dict[str, OAuthClient] = {client.id: client for client in clients or []}

    async def get_by_id(self, client_id: str) -> OAuthClient | None:
        return self._clients.get(client_id)

    async def get_many(self, client_ids: Iterable[str]) -> dict[str, OAuthClient]:
        return {
            client_id: self._clients[client_id]
            for client_id in client_ids
            if client_id in self._clients
        }

    async def save(self, client: OAuthClient) -> None:
        self._clients[client.id] = client

    async def count_unconsented(self) -> int:
        return sum(
            1 for client in self._clients.values() if client.state is OAuthClientState.REGISTERED
        )

    async def evict_oldest_unconsented(self, *, cutoff: datetime) -> None:
        evictable = [
            client
            for client in self._clients.values()
            if client.state is OAuthClientState.REGISTERED and client.created_at < cutoff
        ]
        if not evictable:
            return
        oldest = min(evictable, key=lambda client: client.created_at)
        del self._clients[oldest.id]


class InMemoryAuthorizationRequestRepository:
    def __init__(self) -> None:
        self._requests: dict[uuid.UUID, AuthorizationRequest] = {}

    async def create(self, request: AuthorizationRequest) -> None:
        self._requests[request.id] = request

    async def save(self, request: AuthorizationRequest) -> None:
        self._requests[request.id] = request

    async def get_by_id(self, txn_id: uuid.UUID) -> AuthorizationRequest | None:
        return self._requests.get(txn_id)

    async def get_by_code_hash(self, code_hash: TokenHash) -> AuthorizationRequest | None:
        for request in self._requests.values():
            if request.code_hash == code_hash:
                return request
        return None

    async def count_pending_for_client(self, client_id: str) -> int:
        return sum(
            1
            for request in self._requests.values()
            if request.client_id == client_id and request.state is AuthorizationRequestState.PENDING
        )


class InMemoryGrantRepository:
    def __init__(self) -> None:
        self._grants: dict[uuid.UUID, Grant] = {}

    async def create(self, grant: Grant) -> None:
        self._grants[grant.id] = grant

    async def save(self, grant: Grant) -> None:
        self._grants[grant.id] = grant

    async def get_by_id(self, grant_id: uuid.UUID) -> Grant | None:
        return self._grants.get(grant_id)

    async def get_by_token_hash(self, token_hash: TokenHash) -> Grant | None:
        for grant in self._grants.values():
            if grant.find_token(token_hash) is not None:
                return grant
        return None

    async def get_by_token_hash_for_rotation(self, token_hash: TokenHash) -> Grant | None:
        """Sin Postgres no hay filas que reservar (`FOR UPDATE NOWAIT` es
        un detalle de `SqlGrantRepository`): los tests de `RefreshGrant`
        con esta fake son de un solo caso de uso a la vez, nunca de dos
        transacciones reales disputandose el mismo token."""
        return await self.get_by_token_hash(token_hash)

    async def get_by_authorization_request_id(self, txn_id: uuid.UUID) -> Grant | None:
        for grant in self._grants.values():
            if grant.authorization_request_id == txn_id:
                return grant
        return None

    async def list_active_for_owner(self, owner_id: uuid.UUID) -> list[Grant]:
        return [
            grant
            for grant in self._grants.values()
            if grant.owner_id == owner_id and not grant.is_revoked
        ]


class FakeOAuthSession:
    """Doble de `infrastructure/sql_oauth_session.py::SqlOAuthSession` para
    `test_sdk_provider.py` (tasks.md T009): agrupa las tres fakes de arriba
    tras el mismo `async with`, sin Postgres. `commit()` es un no-op --
    coherente con que las fakes ya escriben directamente en memoria en cada
    llamada, no al confirmar."""

    def __init__(
        self,
        *,
        clients: InMemoryClientRepository | None = None,
        authorization_requests: InMemoryAuthorizationRequestRepository | None = None,
        grants: InMemoryGrantRepository | None = None,
    ) -> None:
        self.clients = clients or InMemoryClientRepository()
        self.authorization_requests = (
            authorization_requests or InMemoryAuthorizationRequestRepository()
        )
        self.grants = grants or InMemoryGrantRepository()

    async def __aenter__(self) -> FakeOAuthSession:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def commit(self) -> None:
        return None
