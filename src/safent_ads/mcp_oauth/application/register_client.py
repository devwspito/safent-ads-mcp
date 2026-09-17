"""`RegisterClient` (contracts/oauth.md §3, tasks.md T005): registro
dinamico de un cliente OAuth. Las reglas RFC 7591 de forma
(`grant_types`/`response_types`) ya las valida el `RegistrationHandler`
del SDK (plan.md; evita duplicar una comprobacion, C-9); este caso de uso
solo aplica politica propia: tope de clientes sin consentir
(threat-model.md C-42) y el secreto hasheado de los clientes
confidenciales.

`ClientRegistration.client_id`/`client_secret` (tasks.md T009): el SDK
(`RegistrationHandler.handle`, `mcp/server/auth/handlers/register.py`) ya
acuña el `client_id` (RFC 7591 `uuid4`) y, si aplica, el `client_secret` en
claro ANTES de llamar a `register_client()`, y la respuesta 201 que el
cliente recibe es ese mismo objeto -- nunca lo que devuelve este caso de
uso. Si el adaptador del SDK (`presentation/sdk_provider.py`) no reenvia
esa identidad ya decidida, el registro quedaria persistido con un
`client_id` que el cliente jamas usara. Sin adaptador (los tests de esta
lane), ambos se generan aqui, igual que antes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from safent_ads.mcp_oauth.application.errors import TooManyUnconsentedClientsError
from safent_ads.mcp_oauth.application.policy import (
    AUTHORIZATION_REQUEST_TTL,
    MAX_UNCONSENTED_CLIENTS,
    MAX_UNCONSENTED_CLIENTS_HARD_CEILING,
)
from safent_ads.mcp_oauth.application.ports import ClientRepository, OpaqueTokenFactory, TokenHasher
from safent_ads.mcp_oauth.domain.client import OAuthClient, RedirectUri, TokenEndpointAuthMethod
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import IdGenerator


@dataclass(frozen=True, slots=True, kw_only=True)
class ClientRegistration:
    client_name: str
    redirect_uris: tuple[str, ...]
    token_endpoint_auth_method: TokenEndpointAuthMethod
    grant_types: tuple[str, ...]
    requested_scope: ScopeSet
    # `None` = generar aqui (tests de esta lane); un valor ya decidido por
    # el SDK de DCR (T009) se respeta tal cual.
    client_id: str | None = None
    client_secret: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RegisteredClient:
    client: OAuthClient
    client_secret: str | None


class RegisterClient:
    def __init__(
        self,
        *,
        clients: ClientRepository,
        id_generator: IdGenerator,
        token_hasher: TokenHasher,
        token_factory: OpaqueTokenFactory,
        clock: Clock,
    ) -> None:
        self._clients = clients
        self._id_generator = id_generator
        self._token_hasher = token_hasher
        self._token_factory = token_factory
        self._clock = clock

    async def execute(self, registration: ClientRegistration) -> RegisteredClient:
        now = self._clock.now()
        await self._evict_oldest_unconsented_if_at_capacity(now)
        client_secret = registration.client_secret or self._issue_secret_if_confidential(
            registration.token_endpoint_auth_method
        )
        client = OAuthClient(
            client_id=registration.client_id or str(self._id_generator.new_id()),
            client_name=registration.client_name,
            redirect_uris=tuple(RedirectUri(uri) for uri in registration.redirect_uris),
            token_endpoint_auth_method=registration.token_endpoint_auth_method,
            client_secret_hash=(
                str(self._token_hasher.hash(client_secret)) if client_secret else None
            ),
            grant_types=registration.grant_types,
            requested_scope=registration.requested_scope,
            created_at=now,
        )
        await self._clients.save(client)
        return RegisteredClient(client=client, client_secret=client_secret)

    async def _evict_oldest_unconsented_if_at_capacity(self, now: datetime) -> None:
        """M3 (threat-model.md C-42): al tope, desaloja al REGISTERED mas
        antiguo en vez de rechazar el registro nuevo -- DCR sigue abierta a
        Internet sin que un atacante pueda cerrarla rellenando el cupo. El
        techo DURO (10x) sigue rechazando: si el desalojo no puede seguir
        el ritmo, mejor un 400 explicito que una tabla sin fondo.

        Nit de la revision de seguridad final (16-sep): solo desaloja
        candidatos con `created_at` anterior a `AUTHORIZATION_REQUEST_TTL`
        -- uno mas joven que eso puede tener una `AuthorizationRequest`
        PENDING en curso, y `evict_oldest_unconsented` no toca nada si
        ninguno califica (el techo duro sigue de respaldo)."""
        count = await self._clients.count_unconsented()
        if count >= MAX_UNCONSENTED_CLIENTS_HARD_CEILING:
            raise TooManyUnconsentedClientsError("limite duro de clientes sin consentir alcanzado")
        if count >= MAX_UNCONSENTED_CLIENTS:
            await self._clients.evict_oldest_unconsented(cutoff=now - AUTHORIZATION_REQUEST_TTL)

    def _issue_secret_if_confidential(self, method: TokenEndpointAuthMethod) -> str | None:
        if method is TokenEndpointAuthMethod.NONE:
            return None
        return self._token_factory.new_token()
