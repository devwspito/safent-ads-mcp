"""`StartAuthorization` (contracts/oauth.md §4, tasks.md T005): crea la
`AuthorizationRequest` PENDING que `GET /authorize` redirige a la pantalla
de consentimiento. No emite codigo todavia -- eso es `ApproveConsent`."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.mcp_oauth.application.client_lookup import (
    get_client_tolerating_domain_violations,
)
from safent_ads.mcp_oauth.application.errors import (
    InvalidTargetError,
    RedirectUriMismatchError,
    TooManyPendingAuthorizationsError,
    UnknownClientError,
)
from safent_ads.mcp_oauth.application.policy import (
    AUTHORIZATION_REQUEST_TTL,
    MAX_PENDING_PER_CLIENT,
)
from safent_ads.mcp_oauth.application.ports import AuthorizationRequestRepository, ClientRepository
from safent_ads.mcp_oauth.domain.authorization import AuthorizationRequest
from safent_ads.mcp_oauth.domain.client import OAuthClient
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import IdGenerator


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthorizationRequestDraft:
    client_id: str
    redirect_uri: str
    code_challenge: str
    client_state: str | None
    requested_scope: str
    resource: str


class StartAuthorization:
    def __init__(
        self,
        *,
        clients: ClientRepository,
        authorization_requests: AuthorizationRequestRepository,
        id_generator: IdGenerator,
        clock: Clock,
        public_base_url: str,
    ) -> None:
        self._clients = clients
        self._authorization_requests = authorization_requests
        self._id_generator = id_generator
        self._clock = clock
        self._canonical_resource = ResourceIndicator.canonical(public_base_url)

    async def execute(self, draft: AuthorizationRequestDraft) -> AuthorizationRequest:
        client = await self._require_client(draft.client_id)
        if client.find_matching_redirect_uri(draft.redirect_uri) is None:
            raise RedirectUriMismatchError("redirect_uri no registrada para este cliente")
        if draft.resource != self._canonical_resource.value:
            raise InvalidTargetError(f"resource no soportado: {draft.resource!r}")
        await self._reject_if_client_at_pending_capacity(draft.client_id)
        return await self._create_request(draft)

    async def _require_client(self, client_id: str) -> OAuthClient:
        client = await get_client_tolerating_domain_violations(self._clients, client_id)
        if client is None:
            raise UnknownClientError(f"cliente desconocido: {client_id}")
        return client

    async def _reject_if_client_at_pending_capacity(self, client_id: str) -> None:
        pending = await self._authorization_requests.count_pending_for_client(client_id)
        if pending >= MAX_PENDING_PER_CLIENT:
            raise TooManyPendingAuthorizationsError(
                f"cliente {client_id} supera {MAX_PENDING_PER_CLIENT} solicitudes pendientes"
            )

    async def _create_request(self, draft: AuthorizationRequestDraft) -> AuthorizationRequest:
        now = self._clock.now()
        request = AuthorizationRequest(
            txn_id=self._id_generator.new_id(),
            client_id=draft.client_id,
            redirect_uri=draft.redirect_uri,
            code_challenge=draft.code_challenge,
            client_state=draft.client_state,
            scope_set=ScopeSet.parse(draft.requested_scope),
            resource=self._canonical_resource,
            created_at=now,
            expires_at=now + AUTHORIZATION_REQUEST_TTL,
        )
        await self._authorization_requests.create(request)
        return request
