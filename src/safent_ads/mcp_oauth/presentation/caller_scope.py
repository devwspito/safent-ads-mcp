"""`OAuthCallerScopeResolver` (spec 002 tasks.md T010; threat-model.md
C-57): implementa `mcp.application.caller_scope.CallerScopeResolverPort` --
el unico punto de contacto `mcp_oauth -> mcp` que produce un `CallerScope`
(plan.md: "Contrato entre contextos").

Fusion con lane/003: `/mcp` ya no delega la autenticacion en el
`RequireAuthMiddleware` del SDK (la sub-app cuelga detras de
`SeatCredentialRouter`, que enruta por permiso), asi que este resolutor
recibe el bearer CRUDO y decide en cadena:

1. El token es una concesion OAuth viva -> `CallerScope` con su alcance
   explicito (`granted_scopes`) y el permiso que ese alcance concede.
2. Cualquier otra cosa -- el bearer estatico `ADS_MCP_TOKEN` o un puesto de
   Enterprise -- se delega en `fallback` (`SingleOwnerCallerScopeResolver` o
   `EnterpriseSeatCallerScopeResolver`), que ya sabe validarlos.

La verificacion del token OAuth la hace el MISMO `CompositeTokenVerifier`
que protege `GET /mcp/health` (threat-model.md C-48: nunca un segundo
camino de verificacion).

`allowed_business_ids` de un token OAuth **nunca** es el comodin implicito:
un cliente registrado por un tercero hereda el conjunto EXPLICITO de
negocios del propietario, enumerado en cada resolucion.

`permission` de un token OAuth nunca llega a `aprobar` (data-model.md §1,
contracts/mcp.md §3.4): aprobar es una decision de empresa que se toma en
el panel con sesion + TOTP, no algo que un agente externo herede de un
alcance OAuth. `ads:propose` concede `proponer`; solo `ads:read`, `ver`."""

from __future__ import annotations

from mcp.server.auth.provider import TokenVerifier

from safent_ads.mcp.application.active_businesses import ActiveBusinessIdsPort
from safent_ads.mcp.application.caller_scope import (
    CallerScope,
    CallerScopeResolverPort,
    Permission,
)
from safent_ads.mcp_oauth.domain.scope import Scope
from safent_ads.mcp_oauth.presentation.token_verifier import STATIC_CALLER_CLIENT_ID

# Etiqueta de persona para la auditoria (`decision_log.person_label`): un
# agente OAuth no es una persona con nombre en Enterprise, pero la fila se
# escribe igual y tiene que decir QUE fue -- nunca vacio.
_OAUTH_AGENT_LABEL = "Agente conectado"


class OAuthCallerScopeResolver:
    def __init__(
        self,
        *,
        token_verifier: TokenVerifier,
        active_businesses: ActiveBusinessIdsPort,
        fallback: CallerScopeResolverPort,
    ) -> None:
        self._token_verifier = token_verifier
        self._active_businesses = active_businesses
        self._fallback = fallback

    async def aclose(self) -> None:
        """`composition/app.py::_build_lifespan` cierra el resolutor de
        puesto al apagar; envolverlo no puede perder ese cierre."""
        aclose = getattr(self._fallback, "aclose", None)
        if aclose is not None:
            await aclose()

    async def resolve(self, bearer_token: str) -> CallerScope:
        access_token = await self._token_verifier.verify_token(bearer_token)
        if access_token is None or access_token.client_id == STATIC_CALLER_CLIENT_ID:
            # El bearer estatico lo resuelve el resolutor de puesto (alcance
            # real sobre los negocios activos), nunca este modulo: aqui solo
            # se reconoce para no inventarle un `CallerScope` paralelo.
            return await self._fallback.resolve(bearer_token)
        scopes = frozenset(access_token.scopes)
        return CallerScope(
            caller_id=f"oauth:{access_token.client_id}:{access_token.subject}",
            allowed_business_ids=await self._active_businesses.active_business_ids(),
            permission=_permission_for(scopes),
            person_label=_OAUTH_AGENT_LABEL,
            granted_scopes=scopes,
        )


def _permission_for(scopes: frozenset[str]) -> Permission:
    if Scope.PROPOSE.value in scopes:
        return Permission.PROPOSE
    return Permission.VIEW
