"""`build_oauth_routes` (tasks.md T013, threat-model.md C-49, C-52): las
rutas HTTP del authorization server (`create_auth_routes`) y del recurso
protegido (`create_protected_resource_routes`) del SDK, montadas
DIRECTAMENTE en el router del padre.

`composition/app.py::_route_oauth_endpoints` las añade ahí porque el SDK
las registraría dentro de la sub-app de `/mcp` si se le pasara
`auth_server_provider` a `MCPServer` -- y esa sub-app es inalcanzable desde
fuera, al estar montada como `Route("/mcp", ...)` exacta, no `Mount`
(plan.md "Cableado exacto en composition/app.py"). Por eso `mcp/presentation/
http.py::build_mcp_server` nunca recibe `auth_server_provider`, solo
`token_verifier`."""

from __future__ import annotations

from typing import Any

from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.provider import OAuthAuthorizationServerProvider
from mcp.server.auth.routes import (
    build_metadata,
    build_resource_metadata_url,
    cors_middleware,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl, ConfigDict, TypeAdapter
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from safent_ads.mcp_oauth.domain.scope import Scope

_SUPPORTED_SCOPES = [Scope.READ.value, Scope.PROPOSE.value]
# L2 de la revision de seguridad (16-sep, minimo privilegio): un cliente
# DCR que registra sin `scope` explicito recibe SOLO lectura --
# `valid_scopes` (arriba) sigue permitiendo ambos cuando el cliente los
# pide de verdad en `/authorize`.
_DEFAULT_SCOPES = [Scope.READ.value]
_RESOURCE_SCOPES = [Scope.READ.value]
_METADATA_PATH = "/.well-known/oauth-authorization-server"
# RFC 9728 SS3.1: la ruta "por recurso" (`/mcp` colgado detras) es la que
# `create_protected_resource_routes()` registra y la que el 401 de `/mcp`
# anuncia en `WWW-Authenticate: resource_metadata=`. T049 (spec 008): varios
# clientes (Codex incluido) prueban ANTES la ruta pelada -- sin una ruta
# explicita para ella cae en el catch-all de SPA (`_mount_panel_spa`) y
# responde 200 con el HTML del panel, nunca JSON.
_BARE_PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"
_WELL_KNOWN_PREFIX = "/.well-known/"

# RFC 8414 SS2 / RFC 9207: el SDK 2.2 no anuncia ninguno de los dos por
# defecto (`mcp:server/auth/routes.py::build_metadata`) -- C-49 exige
# ambos para que Claude Code/Codex descubran DCR publica y el AS confirme
# su propia identidad en la redireccion de vuelta.
#
# M2 de la revision de seguridad (16-sep): SOLO `none` -- `register_client()`
# (`SdkOAuthProvider._require_public_client()`) rechaza cualquier otro
# `token_endpoint_auth_method`, asi que anunciar `client_secret_post`/
# `client_secret_basic` era una promesa falsa (un cliente RFC 8414 estricto
# los intentaria y siempre chocaria con `invalid_client_metadata`).
_TOKEN_ENDPOINT_AUTH_METHODS_SUPPORTED = ["none"]

# `AnyHttpUrl("https://host")` a secas normaliza a "https://host/" (barra
# final anadida): rompe la comparacion de cadena exacta que RFC 8414 exige
# para el `issuer` (threat-model.md C-49, plan.md "Ajustes"). Solo un
# `TypeAdapter` con `url_preserve_empty_path=True` (el mismo `model_config`
# que ya trae `AuthSettings`, `mcp/server/auth/settings.py`) conserva la
# ausencia de path tal cual.
_ISSUER_URL_ADAPTER: TypeAdapter[AnyHttpUrl] = TypeAdapter(
    AnyHttpUrl, config=ConfigDict(url_preserve_empty_path=True)
)


def issuer_url(public_base_url: str) -> AnyHttpUrl:
    """Unico punto que construye el `AnyHttpUrl` del emisor (C-49): tanto
    `build_oauth_routes` como `composition/app.py::_build_mcp_oauth_wiring`
    (para `AuthSettings.issuer_url`) tienen que preservar la ausencia de
    path exactamente igual, o el emisor que anuncia el AS y el que valida
    `AuthSettings` divergirian en una barra."""
    return _ISSUER_URL_ADAPTER.validate_python(public_base_url)


def build_oauth_routes(
    provider: OAuthAuthorizationServerProvider[Any, Any, Any],
    *,
    public_base_url: str,
    resource_name: str,
) -> list[Route]:
    """`/.well-known/oauth-authorization-server`, `/authorize`, `/token`,
    `/register`, `/revoke` (`create_auth_routes`) + las dos rutas de
    RFC 9728 SS3.1 (`/.well-known/oauth-protected-resource/mcp` y la
    pelada, `_bare_protected_resource_metadata_route`).
    `composition/app.py` las cuelga del router padre antes de
    `_mount_panel_spa` y antes de `harden_api` (C-52) -- solo si
    `ADS_MCP_OAUTH_ENABLED`; `well_known_not_found_route()` (el catch-all
    de 404/405 JSON) se registra APARTE, siempre, este activado o no.

    `resource_name` es `InstanceIdentity.name` (`ADS_INSTANCE_NAME`,
    `composition/settings.py`): metadato de presentacion RFC 9728, ningun
    cliente lo usa como clave -- cada instalacion fija el suyo (plan.md §2)."""
    resolved_issuer_url = issuer_url(public_base_url)
    client_registration_options = ClientRegistrationOptions(
        enabled=True, valid_scopes=_SUPPORTED_SCOPES, default_scopes=_DEFAULT_SCOPES
    )
    revocation_options = RevocationOptions(enabled=True)
    auth_routes = create_auth_routes(
        provider,
        issuer_url=resolved_issuer_url,
        client_registration_options=client_registration_options,
        revocation_options=revocation_options,
    )
    _patch_authorization_server_metadata_route(
        auth_routes,
        issuer_url=resolved_issuer_url,
        client_registration_options=client_registration_options,
        revocation_options=revocation_options,
    )
    resource_url = AnyHttpUrl(f"{public_base_url}/mcp")
    resource_routes = create_protected_resource_routes(
        resource_url=resource_url,
        authorization_servers=[resolved_issuer_url],
        scopes_supported=_RESOURCE_SCOPES,
        resource_name=resource_name,
    )
    bare_resource_route = _bare_protected_resource_metadata_route(
        resource_routes, resource_url=resource_url
    )
    return [*auth_routes, *resource_routes, bare_resource_route]


def _bare_protected_resource_metadata_route(
    resource_routes: list[Route], *, resource_url: AnyHttpUrl
) -> Route:
    """`create_protected_resource_routes()` solo registra la ruta "por
    recurso" de RFC 9728 SS3.1 (`build_resource_metadata_url`); un cliente
    que prueba antes la ruta pelada (T049) debe recibir los MISMOS
    metadatos, no el 200 HTML del panel. Reutiliza el endpoint ya envuelto
    en CORS de esa ruta (`ProtectedResourceMetadataHandler.handle`, sin
    estado sobre la peticion) -- una segunda implementacion del mismo JSON
    podria divergir con el tiempo."""
    per_resource_path = build_resource_metadata_url(resource_url).path
    for route in resource_routes:
        if route.path == per_resource_path:
            return Route(
                _BARE_PROTECTED_RESOURCE_PATH, endpoint=route.endpoint, methods=["GET", "OPTIONS"]
            )
    # Mismo criterio que `_patch_authorization_server_metadata_route`:
    # fallar alto si el SDK deja de registrar la ruta esperada, en vez de
    # servir la ruta pelada sin metadatos.
    raise RuntimeError(
        f"{per_resource_path} no aparece en las rutas de create_protected_resource_routes()"
    )


_WELL_KNOWN_CATCH_ALL_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
_GET_LIKE_METHODS = frozenset({"GET", "HEAD"})


def _well_known_envelope(*, status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": {}}},
        headers={"Cache-Control": "no-store"},
    )


async def _well_known_metadata_not_found(request: Request) -> JSONResponse:
    # Revision de seguridad (PR 44): un metodo que no sea GET/HEAD contra
    # `/.well-known/*` es un 405 JSON (con el MISMO sobre que el resto de
    # la API), no el texto plano que Starlette devuelve por defecto para
    # una ruta cuyo metodo no casa -- ni tampoco el 200 HTML del panel.
    if request.method not in _GET_LIKE_METHODS:
        return _well_known_envelope(
            status_code=405,
            code="METHOD_NOT_ALLOWED",
            message=f"Metodo {request.method} no admitido en {request.url.path}.",
        )
    return _well_known_envelope(
        status_code=404,
        code="NOT_FOUND",
        message="Este servidor no publica ese documento de metadatos.",
    )


def well_known_not_found_route() -> Route:
    """Catch-all de `/.well-known/*` (T049 spec 008 + revision de
    seguridad PR 44): sin esta ruta, CUALQUIER documento de metadatos que
    este AS no publica -- `openid-configuration` (no somos un OIDC
    provider, RFC 8414 SS3 es un protocolo distinto), o el mismo
    `/.well-known/oauth-authorization-server` cuando `ADS_MCP_OAUTH_
    ENABLED=false` -- caia en el catch-all de SPA (`_mount_panel_spa`) y
    respondia 200 con el HTML del panel.

    Publica (sin guion bajo) y NO colgada de `build_oauth_routes()`:
    `composition/app.py::_register_mcp_oauth_surface` la registra SIEMPRE,
    con OAuth activado o no -- el panel nunca debe responder por
    `/.well-known/*`, sea cual sea `ADS_MCP_OAUTH_ENABLED`. Cuando OAuth
    esta activo se registra DESPUES de las rutas concretas (`/.well-known/
    oauth-authorization-server`, las dos de `oauth-protected-resource`):
    Starlette resuelve por orden de registro, asi que esas siguen ganando;
    esta solo atrapa lo que ninguna de ellas sirvio."""
    return Route(
        f"{_WELL_KNOWN_PREFIX}{{path:path}}",
        endpoint=_well_known_metadata_not_found,
        methods=list(_WELL_KNOWN_CATCH_ALL_METHODS),
    )


def _patch_authorization_server_metadata_route(
    routes: list[Route],
    *,
    issuer_url: AnyHttpUrl,
    client_registration_options: ClientRegistrationOptions,
    revocation_options: RevocationOptions,
) -> None:
    """C-49: reconstruye los mismos metadatos que `create_auth_routes` ya
    calculo con `build_metadata()` (la misma funcion, no una segunda
    implementacion) y sustituye solo la ruta de metadatos por una que los
    sirve ampliados con `none` y `authorization_response_iss_parameter_supported`.

    `revocation_endpoint_auth_methods_supported` recibe la MISMA lista que
    `token_endpoint_auth_methods_supported`: ambos endpoints comparten
    `ClientAuthenticator.authenticate_request()`
    (`sdk:middleware/client_auth.py`), y `SdkOAuthProvider._require_public_client()`
    nunca registra otra cosa que clientes `none` (RFC 8252 SS8.4) -- sin
    `none` aqui, un cliente RFC 8414 estricto creeria que `/revoke` exige un
    secreto que el AS nunca emitio (encontrado por
    `tests/contracts/mcp_oauth/test_metadata_documents.py`)."""
    metadata = build_metadata(issuer_url, None, client_registration_options, revocation_options)
    metadata.token_endpoint_auth_methods_supported = list(_TOKEN_ENDPOINT_AUTH_METHODS_SUPPORTED)
    metadata.revocation_endpoint_auth_methods_supported = list(
        _TOKEN_ENDPOINT_AUTH_METHODS_SUPPORTED
    )
    metadata.authorization_response_iss_parameter_supported = True
    patched_route = Route(
        _METADATA_PATH,
        endpoint=cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
        methods=["GET", "OPTIONS"],
    )
    for index, route in enumerate(routes):
        if route.path == _METADATA_PATH:
            routes[index] = patched_route
            return
    # I3 de la revision de seguridad (16-sep): si `create_auth_routes` del
    # SDK deja de registrar esta ruta (cambio de version, refactor propio),
    # C-49 (`none` + `authorization_response_iss_parameter_supported`)
    # dejaria de aplicarse EN SILENCIO -- fallar alto en vez de servir los
    # metadatos sin parchear.
    raise RuntimeError(f"{_METADATA_PATH} no aparece en las rutas de create_auth_routes()")
