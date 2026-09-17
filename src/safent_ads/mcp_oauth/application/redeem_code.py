"""`RedeemCode` (contracts/oauth.md §6, threat-model.md C-38): canjea un
codigo de autorizacion de un solo uso por un par de tokens. Reusar un
codigo ya canjeado revoca la concesion que nacio de el.

Resuelto en 0035_mcp_oauth (database-engineer, 16-sep-2026):
`oauth_grants.authorization_txn_id` (UNIQUE, FK SET NULL) es la columna que
`get_by_authorization_request_id` necesita. La atomicidad del canje unico
la pone `SqlAuthorizationRequestRepository.save()` (tasks.md T006): un
`UPDATE ... WHERE state='CONSENTED' RETURNING` que, si no afecta filas
(la ganadora de la carrera ya lo marco REDEEMED), levanta
`CodeAlreadyRedeemedError` -- por eso `save()` esta DENTRO del `try` de
abajo, no solo `request.redeem()`.

`execute()` vs `execute_with_pkce_verified_upstream()` (tasks.md T009): el
SDK (`sdk:handlers/token.py:185-196`) ya calcula
`sha256(code_verifier)` y lo compara contra el `code_challenge` que
`load_authorization_code()` (presentation/sdk_provider.py) le devolvio --
el mismo valor que esta clase guarda -- **antes** de llamar a
`exchange_authorization_code()`, que es el unico punto de entrada real a
este caso de uso en produccion; `code_verifier` ni siquiera viaja hasta
ahi (`sdk:provider.py::exchange_authorization_code` no lo recibe). Repetir
la comprobacion con un dato que no existe no es posible, y omitirla con un
parametro opcional (`code_verifier: str | None`) seria el antipatron de
bandera que cambia el comportamiento de una misma funcion -- de ahi el
metodo separado, con el mismo camino compartido salvo ese paso."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from safent_ads.mcp_oauth.application.errors import (
    ClientMismatchError,
    InvalidTargetError,
    PkceVerificationError,
    RedirectUriMismatchError,
    UnknownAuthorizationCodeError,
)
from safent_ads.mcp_oauth.application.policy import ACCESS_TOKEN_TTL
from safent_ads.mcp_oauth.application.ports import (
    AuthorizationRequestRepository,
    GrantRepository,
    OpaqueTokenFactory,
    TokenHasher,
)
from safent_ads.mcp_oauth.application.token_issuance import mint_access_and_refresh
from safent_ads.mcp_oauth.domain.authorization import AuthorizationRequest
from safent_ads.mcp_oauth.domain.errors import CodeAlreadyRedeemedError
from safent_ads.mcp_oauth.domain.grant import Grant
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import IdGenerator


@dataclass(frozen=True, slots=True, kw_only=True)
class IssuedTokenPair:
    access_token: str
    refresh_token: str
    scope_set: ScopeSet
    expires_in: timedelta


class RedeemCode:
    def __init__(
        self,
        *,
        authorization_requests: AuthorizationRequestRepository,
        grants: GrantRepository,
        token_hasher: TokenHasher,
        token_factory: OpaqueTokenFactory,
        id_generator: IdGenerator,
        clock: Clock,
    ) -> None:
        self._authorization_requests = authorization_requests
        self._grants = grants
        self._token_hasher = token_hasher
        self._token_factory = token_factory
        self._id_generator = id_generator
        self._clock = clock

    async def execute(
        self, *, code: str, redirect_uri: str, client_id: str, code_verifier: str, resource: str
    ) -> IssuedTokenPair:
        request = await self._find_and_bind(code, redirect_uri, client_id, resource)
        if not request.verify_pkce(code_verifier):
            raise PkceVerificationError("code_verifier no coincide con code_challenge")
        return await self._consume(request)

    async def execute_with_pkce_verified_upstream(
        self, *, code: str, redirect_uri: str, client_id: str, resource: str
    ) -> IssuedTokenPair:
        """Mismo canje que `execute()`, sin repetir una verificacion de PKCE
        que ya hizo el llamador (el SDK, ver docstring del modulo)."""
        request = await self._find_and_bind(code, redirect_uri, client_id, resource)
        return await self._consume(request)

    async def _find_and_bind(
        self, code: str, redirect_uri: str, client_id: str, resource: str
    ) -> AuthorizationRequest:
        request = await self._find_request(code)
        self._validate_binding(request, redirect_uri, client_id, resource)
        return request

    async def _consume(self, request: AuthorizationRequest) -> IssuedTokenPair:
        now = self._clock.now()
        try:
            request.redeem(now)
            await self._authorization_requests.save(request)
        except CodeAlreadyRedeemedError:
            # `save()` (SQL: `UPDATE ... WHERE state='CONSENTED' RETURNING`,
            # tasks.md T006) es quien detecta el replay entre dos canjes
            # concurrentes -- el objeto en memoria de este proceso ya habia
            # transicionado a REDEEMED sin ver al otro. La atomicidad la
            # pone la base de datos, no `request.redeem()` (plan.md).
            await self._revoke_issued_family(request, now)
            raise
        return await self._issue_grant(request, now)

    async def _find_request(self, code: str) -> AuthorizationRequest:
        code_hash = self._token_hasher.hash(code)
        request = await self._authorization_requests.get_by_code_hash(code_hash)
        if request is None:
            raise UnknownAuthorizationCodeError("codigo de autorizacion desconocido")
        return request

    @staticmethod
    def _validate_binding(
        request: AuthorizationRequest, redirect_uri: str, client_id: str, resource: str
    ) -> None:
        if request.client_id != client_id:
            raise ClientMismatchError("client_id no coincide con la solicitud")
        if request.redirect_uri != redirect_uri:
            raise RedirectUriMismatchError("redirect_uri no coincide con la solicitud")
        if request.resource.value != resource:
            raise InvalidTargetError(f"resource no coincide con el canonico: {resource!r}")

    async def _revoke_issued_family(self, request: AuthorizationRequest, now: datetime) -> None:
        grant = await self._grants.get_by_authorization_request_id(request.id)
        if grant is not None and not grant.is_revoked:
            grant.revoke(now=now, reason="authorization_code_replayed")
            await self._grants.save(grant)

    async def _issue_grant(self, request: AuthorizationRequest, now: datetime) -> IssuedTokenPair:
        assert request.owner_id is not None  # noqa: S101 - invariante: CONSENTED siempre fija owner_id
        access, refresh = mint_access_and_refresh(
            now=now, factory=self._token_factory, hasher=self._token_hasher
        )
        grant = Grant(
            grant_id=self._id_generator.new_id(),
            authorization_request_id=request.id,
            owner_id=request.owner_id,
            client_id=request.client_id,
            scope_set=request.scope_set,
            resource=request.resource,
            created_at=now,
            tokens=(access.issued, refresh.issued),
        )
        await self._grants.create(grant)
        return IssuedTokenPair(
            access_token=access.raw_value,
            refresh_token=refresh.raw_value,
            scope_set=grant.scope_set,
            expires_in=ACCESS_TOKEN_TTL,
        )
