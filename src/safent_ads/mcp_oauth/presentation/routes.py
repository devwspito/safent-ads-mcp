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
    cors_middleware,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl, ConfigDict, TypeAdapter
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
    `/register`, `/revoke` (`create_auth_routes`) +
    `/.well-known/oauth-protected-resource/mcp` (`create_protected_resource_routes`).
    `composition/app.py` las cuelga del router padre antes de
    `_mount_panel_spa` y antes de `harden_api` (C-52).

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
    resource_routes = create_protected_resource_routes(
        resource_url=AnyHttpUrl(f"{public_base_url}/mcp"),
        authorization_servers=[resolved_issuer_url],
        scopes_supported=_RESOURCE_SCOPES,
        resource_name=resource_name,
    )
    return [*auth_routes, *resource_routes]


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
