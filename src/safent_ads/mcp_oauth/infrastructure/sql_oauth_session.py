"""`SqlOAuthSession` (tasks.md T009): una sesion SQLAlchemy y sus tres
repositorios agrupados, para que `presentation/sdk_provider.py` abra
exactamente una transaccion por llamada del SDK (mismo patron que
`composition/mcp_write_adapter.py`).

No comete ni deshace por su cuenta al salir del `async with`: cada metodo
de `SdkOAuthProvider` decide cuando confirmar, porque algunos deben
persistir un efecto secundario (revocar la concesion emitida, C-38) incluso
cuando terminan traduciendo la llamada a un error OAuth. Salir sin commit
explicito deshace la transaccion (comportamiento por defecto de
`AsyncSession.__aexit__`) -- fallo cerrado."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.mcp_oauth.application.ports import (
    AuthorizationRequestRepository,
    ClientRepository,
    GrantRepository,
    OAuthSession,
)
from safent_ads.mcp_oauth.infrastructure.sql_authorization_repository import (
    SqlAuthorizationRequestRepository,
)
from safent_ads.mcp_oauth.infrastructure.sql_client_repository import SqlClientRepository
from safent_ads.mcp_oauth.infrastructure.sql_grant_repository import SqlGrantRepository
from safent_ads.shared.clock import Clock


class SqlOAuthSession:
    # Anotados como los PUERTOS (`application/ports.py`), no los adaptadores
    # SQL concretos: un atributo mutable de un `Protocol` se compara de
    # forma INVARIANTE, no covariante -- `clients: SqlClientRepository`
    # rompe la conformidad estructural con `OAuthSession.clients:
    # ClientRepository`, aunque `SqlClientRepository` implemente ese puerto.
    clients: ClientRepository
    authorization_requests: AuthorizationRequestRepository
    grants: GrantRepository

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], clock: Clock
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._session: AsyncSession | None = None

    # Anotado como el `OAuthSession` Protocol, no `Self`: `OAuthSessionFactory`
    # (`application/ports.py`) es `Callable[[], AbstractAsyncContextManager[
    # OAuthSession]]` -- devolver `Self` en la firma hace que mypy compare la
    # firma EXACTA del miembro del protocolo con `OAuthSession` en vez de
    # aceptar la covarianza de retorno.
    async def __aenter__(self) -> OAuthSession:
        self._session = self._session_factory()
        await self._session.__aenter__()
        self.clients = SqlClientRepository(self._session)
        self.authorization_requests = SqlAuthorizationRequestRepository(
            self._session, clock=self._clock
        )
        self.grants = SqlGrantRepository(self._session)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        session = self._session
        assert session is not None  # noqa: S101 - __aenter__ siempre lo fija antes
        await session.__aexit__(exc_type, exc, tb)

    async def commit(self) -> None:
        session = self._session
        assert session is not None  # noqa: S101 - solo se llama dentro del `async with`
        await session.commit()
