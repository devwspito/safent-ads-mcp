"""Puertos de `mcp_oauth` (plan.md N0): `application` depende de
abstracciones, nunca de SQLAlchemy/hashlib/secrets directamente. `Clock`
se reutiliza de `shared/` (mismo puerto que `iam`).

`OAuthSession`/`OAuthSessionFactory` (B2 de la revision de seguridad,
16-sep): vivian en `presentation/oauth_session.py`, lo que obligaba a
`infrastructure/sql_oauth_session.py` a importar de `presentation` bajo
`TYPE_CHECKING` para anotar su tipo de retorno -- una dependencia
`infrastructure -> presentation` que invierte la direccion correcta
(presentation -> application <- infrastructure). Viven aqui, en
`application`, para que tanto `presentation/sdk_provider.py`/
`token_verifier.py` como `infrastructure/sql_oauth_session.py` dependan
del mismo puerto sin que ninguno de los dos dependa del otro."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol

from safent_ads.mcp_oauth.domain.authorization import AuthorizationRequest
from safent_ads.mcp_oauth.domain.client import OAuthClient
from safent_ads.mcp_oauth.domain.grant import Grant, TokenHash


class ClientRepository(Protocol):
    async def get_by_id(self, client_id: str) -> OAuthClient | None: ...
    async def get_many(self, client_ids: Iterable[str]) -> dict[str, OAuthClient]:
        """I7 (revision de seguridad, 16-sep): una sola consulta para
        varios `client_id` -- `ListGrants` la usa para no pedir el cliente
        de cada concesion uno a uno (2N+1). Claves ausentes de la
        concesion en el resultado significan "cliente ya no existe", el
        mismo caso que `get_by_id()` devolviendo `None`."""
        ...
    async def save(self, client: OAuthClient) -> None: ...
    async def count_unconsented(self) -> int: ...
    async def evict_oldest_unconsented(self, *, cutoff: datetime) -> None:
        """M3 (threat-model.md C-42): borra el cliente REGISTERED (nunca
        consentido) mas antiguo, si hay alguno con `created_at < cutoff`.
        `CASCADE` (0035_mcp_oauth) se lleva por delante sus
        `oauth_authorization_requests` -- seguro para un REGISTERED que
        nunca tuvo consentimiento, pero no para uno con una
        `AuthorizationRequest` PENDING en curso: `cutoff` (llamante:
        `RegisterClient`, `now - AUTHORIZATION_REQUEST_TTL`) excluye a
        cualquier cliente lo bastante joven para tener una en vuelo."""
        ...


class AuthorizationRequestRepository(Protocol):
    async def create(self, request: AuthorizationRequest) -> None: ...
    async def save(self, request: AuthorizationRequest) -> None: ...
    async def get_by_id(self, txn_id: uuid.UUID) -> AuthorizationRequest | None: ...
    async def get_by_code_hash(self, code_hash: TokenHash) -> AuthorizationRequest | None: ...
    async def count_pending_for_client(self, client_id: str) -> int: ...


class GrantRepository(Protocol):
    async def create(self, grant: Grant) -> None: ...
    async def save(self, grant: Grant) -> None: ...
    async def get_by_id(self, grant_id: uuid.UUID) -> Grant | None: ...
    async def get_by_token_hash(self, token_hash: TokenHash) -> Grant | None: ...
    async def get_by_token_hash_for_rotation(self, token_hash: TokenHash) -> Grant | None:
        """Como `get_by_token_hash`, pero SOLO para `RefreshGrant`
        (fix/refresh-rotation-race): reserva la fila del refresh
        presentado para ESTA rotacion antes de leerla, para que una
        segunda rotacion concurrente del MISMO token nunca la relea a
        medio camino -- `ConcurrentRefreshInProgressError` si otra
        transaccion ya la tiene reservada."""
        ...

    async def get_by_authorization_request_id(self, txn_id: uuid.UUID) -> Grant | None: ...
    async def list_active_for_owner(self, owner_id: uuid.UUID) -> list[Grant]: ...


class TokenHasher(Protocol):
    def hash(self, raw_token: str) -> TokenHash: ...


class OpaqueTokenFactory(Protocol):
    def new_token(self) -> str: ...


class OAuthSession(Protocol):
    """Lo que `sdk_provider.py`/`token_verifier.py` necesitan de una
    transaccion: los tres repositorios de arriba y un `commit()` explicito.
    `infrastructure/sql_oauth_session.py::SqlOAuthSession` lo implementa
    sobre Postgres; `infrastructure/fakes.py::FakeOAuthSession` en memoria
    para los tests de esta capa."""

    clients: ClientRepository
    authorization_requests: AuthorizationRequestRepository
    grants: GrantRepository

    async def commit(self) -> None: ...


OAuthSessionFactory = Callable[[], AbstractAsyncContextManager[OAuthSession]]
