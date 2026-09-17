"""`CompositeTokenVerifier` (tasks.md T010, T010+; threat-model.md C-48,
C-53): la unica implementacion de `TokenVerifier` que autentica `/mcp` y
`GET /mcp/health` -- primero por hash contra una concesion OAuth viva, si
no por comparacion en tiempo constante contra `ADS_MCP_TOKEN` (via
`shared/bearer.py`, C-9: nunca una segunda implementacion de esa
comprobacion).

Ambos conmutadores (`ADS_MCP_OAUTH_ENABLED`, `ADS_MCP_STATIC_TOKEN_ENABLED`)
se expresan como dependencias OPCIONALES, no como banderas booleanas que
ramifican el mismo metodo: `session_factory=None` apaga la rama OAuth,
`static_token=None` apaga la rama estatica. `composition/app.py` (T013/T014)
decide cual construir segun `ApiSettings`."""

from __future__ import annotations

from mcp.server.auth.provider import AccessToken, TokenVerifier

from safent_ads.mcp_oauth.application.introspect_token import IntrospectToken, TokenIntrospection
from safent_ads.mcp_oauth.application.ports import OAuthSessionFactory, TokenHasher
from safent_ads.mcp_oauth.domain.scope import Scope
from safent_ads.shared.bearer import is_token_valid
from safent_ads.shared.clock import Clock

# threat-model.md C-53: identidad sintetica del bearer estatico, alcance
# completo y sin caducidad -- via de emergencia mientras los agentes
# migran a OAuth. Nit de la revision de seguridad (16-sep): un unico
# nombre para el llamador estatico, reutilizado tal cual como `caller_id`
# en `presentation/caller_scope.py::OAuthCallerScopeResolver` -- antes
# cada modulo inventaba su propia variante ("static-owner-token" aqui,
# "owner-static" alli).
STATIC_CALLER_CLIENT_ID = "static-owner"  # noqa: S105 - identificador, no un secreto
# Nit: derivado del enum `Scope`, no una lista de cadenas repetida a mano
# -- si `Scope` gana un tercer valor algun dia, esto no se queda corto en
# silencio.
_STATIC_SCOPES = [scope.value for scope in Scope]


class CompositeTokenVerifier(TokenVerifier):
    def __init__(
        self,
        *,
        session_factory: OAuthSessionFactory | None,
        token_hasher: TokenHasher,
        clock: Clock,
        static_token: str | None,
        resource: str,
    ) -> None:
        self._session_factory = session_factory
        self._token_hasher = token_hasher
        self._clock = clock
        self._static_token = static_token
        self._resource = resource

    async def verify_token(self, token: str) -> AccessToken | None:
        oauth_token = await self._verify_oauth(token)
        if oauth_token is not None:
            return oauth_token
        return self._verify_static(token)

    async def _verify_oauth(self, token: str) -> AccessToken | None:
        if self._session_factory is None:
            return None
        async with self._session_factory() as db:
            use_case = IntrospectToken(
                grants=db.grants, token_hasher=self._token_hasher, clock=self._clock
            )
            introspection = await use_case.execute(token)
        if not introspection.active:
            return None
        return introspection_to_access_token(token, introspection)

    def _verify_static(self, token: str) -> AccessToken | None:
        if self._static_token is None or not is_token_valid(token, self._static_token):
            return None
        return AccessToken(
            token=token,
            client_id=STATIC_CALLER_CLIENT_ID,
            scopes=list(_STATIC_SCOPES),
            expires_at=None,
            resource=self._resource,
        )


def introspection_to_access_token(raw_token: str, introspection: TokenIntrospection) -> AccessToken:
    """Compartido con `presentation/sdk_provider.py::load_access_token`
    (RFC 7009, solo lo usa `RevocationHandler`): una unica traduccion de
    `TokenIntrospection` al `AccessToken` del SDK."""
    assert introspection.client_id is not None  # noqa: S101 - active=True los fija siempre
    assert introspection.scope_set is not None  # noqa: S101 - idem
    assert introspection.resource is not None  # noqa: S101 - idem
    assert introspection.expires_at is not None  # noqa: S101 - idem
    assert introspection.owner_id is not None  # noqa: S101 - idem
    return AccessToken(
        token=raw_token,
        client_id=introspection.client_id,
        scopes=[scope.value for scope in introspection.scope_set.scopes],
        expires_at=int(introspection.expires_at.timestamp()),
        resource=introspection.resource.value,
        subject=str(introspection.owner_id),
    )
