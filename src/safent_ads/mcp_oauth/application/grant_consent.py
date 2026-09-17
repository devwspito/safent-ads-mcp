"""`ApproveConsent`/`DenyConsent` (contracts/oauth.md §5, tasks.md T005):
el acto del propietario que convierte una `AuthorizationRequest` PENDING
en codigo canjeable, o la cierra denegada. El TOTP fresco por transaccion
(`X-Reauth-Token`, threat-model.md C-41) es de presentacion (T015+), no de
este caso de uso.

`ApproveConsent` tambien marca el `OAuthClient` como `TRUSTED`
(data-model.md: "REGISTERED -> (primer consentimiento) TRUSTED"): es el
UNICO punto de la aplicacion donde ocurre "el primer consentimiento", asi
que es donde tiene que fijarse `last_seen_at` -- de lo contrario
`prune_stale_clients.py` (T016) podaria por "sin consentir" un cliente con
concesiones vivas (`oauth_grants.client_id ON DELETE CASCADE` se llevaria
por delante sus tokens)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from safent_ads.mcp_oauth.application.client_lookup import (
    get_client_tolerating_domain_violations,
)
from safent_ads.mcp_oauth.application.errors import (
    AuthorizationRequestNotFoundError,
    UnknownClientError,
)
from safent_ads.mcp_oauth.application.policy import AUTHORIZATION_CODE_TTL
from safent_ads.mcp_oauth.application.ports import (
    AuthorizationRequestRepository,
    ClientRepository,
    OpaqueTokenFactory,
    TokenHasher,
)
from safent_ads.mcp_oauth.domain.authorization import AuthorizationRequest
from safent_ads.mcp_oauth.domain.client import OAuthClient
from safent_ads.shared.clock import Clock


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsentApproved:
    redirect_uri: str
    code: str
    client_state: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsentDenied:
    redirect_uri: str
    client_state: str | None


async def _require_request(
    repository: AuthorizationRequestRepository, txn_id: uuid.UUID
) -> AuthorizationRequest:
    request = await repository.get_by_id(txn_id)
    if request is None:
        raise AuthorizationRequestNotFoundError(f"solicitud {txn_id} no encontrada")
    return request


class ApproveConsent:
    def __init__(
        self,
        *,
        authorization_requests: AuthorizationRequestRepository,
        clients: ClientRepository,
        token_hasher: TokenHasher,
        token_factory: OpaqueTokenFactory,
        clock: Clock,
    ) -> None:
        self._authorization_requests = authorization_requests
        self._clients = clients
        self._token_hasher = token_hasher
        self._token_factory = token_factory
        self._clock = clock

    async def execute(self, *, txn_id: uuid.UUID, owner_id: uuid.UUID) -> ConsentApproved:
        request = await _require_request(self._authorization_requests, txn_id)
        # Fail-closed (I-2, revision de seguridad 17-sep): `sql_client_
        # repository.py` declara "autorizar y consentir fallan cerrado" --
        # una fila que ya no cumple una regla del dominio (destino remoto
        # de D-11, `client_name` con caracteres bidireccionales de C-70
        # pieza 3) se trata como cliente AUSENTE, y consentir se RECHAZA
        # entero. Antes de tocar `request`/emitir ningun codigo: tolerar
        # esto solo tiene sentido en la LECTURA (`GET /consent/{txn}`,
        # `get_client_tolerating_domain_violations`), nunca en un acto que
        # concede acceso de verdad.
        client = await self._require_valid_client(request.client_id)
        code = self._token_factory.new_token()
        now = self._clock.now()
        request.consent(
            owner_id=owner_id,
            code_hash=self._token_hasher.hash(code),
            now=now,
            code_ttl=AUTHORIZATION_CODE_TTL,
        )
        await self._authorization_requests.save(request)
        client.mark_trusted(now)
        await self._clients.save(client)
        return ConsentApproved(
            redirect_uri=request.redirect_uri, code=code, client_state=request.client_state
        )

    async def _require_valid_client(self, client_id: str) -> OAuthClient:
        client = await get_client_tolerating_domain_violations(self._clients, client_id)
        if client is None:
            raise UnknownClientError(f"cliente desconocido: {client_id}")
        return client


class DenyConsent:
    def __init__(
        self, *, authorization_requests: AuthorizationRequestRepository, clock: Clock
    ) -> None:
        self._authorization_requests = authorization_requests
        self._clock = clock

    async def execute(self, *, txn_id: uuid.UUID) -> ConsentDenied:
        request = await _require_request(self._authorization_requests, txn_id)
        request.deny(self._clock.now())
        await self._authorization_requests.save(request)
        return ConsentDenied(redirect_uri=request.redirect_uri, client_state=request.client_state)
