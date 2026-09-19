"""Montaje del transporte MCP en `/mcp` (contracts/mcp.md): tres
`MCPServer` sin estado (`stateless_http=True, json_response=True`), uno por
permiso (`ver`/`proponer`/`aprobar`), sobre el mismo `ToolDispatcher` y el
mismo `Container`. `SeatCredentialRouter` resuelve la credencial UNA vez
por peticion HTTP contra Enterprise (`SeatAuthorityPort`) y delega en el
sub-app del permiso resuelto -- la sesion MCP no es autoridad: revocar
corta en la llamada siguiente, siempre (contracts/mcp.md §1-§2).

Spec 002 (mcp_oauth): el bearer que llega a `SeatCredentialRouter` puede
ser un token OAuth 2.1 propio, un puesto de Enterprise o el bearer estatico
`ADS_MCP_TOKEN` del modo de un solo propietario -- quien distingue los tres
es la CADENA de resolutores (`OAuthCallerScopeResolver` delante del
resolutor de puesto, `composition/app.py`), no este modulo: aqui solo se
extrae el bearer una vez y se traduce el fallo al 401 de RFC 9728, con la
cabecera `WWW-Authenticate` que le dice al cliente MCP donde esta el
servidor de autorizacion para iniciar el flujo (threat-model.md C-48: el
mismo cuerpo y la misma cabecera que `GET /mcp/health`)."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

import structlog
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from safent_ads import __version__
from safent_ads.mcp.application.caller_scope import (
    CallerScopeResolverPort,
    IntrospectionQuotaExceededError,
    Permission,
)
from safent_ads.mcp.application.seat_authority import (
    SeatAuthorityDeniedError,
    SeatAuthorityUnavailableError,
)
from safent_ads.mcp.presentation.dispatcher import ToolDispatcher
from safent_ads.mcp.presentation.mount import CALLER_SCOPE_STATE_ATTR, mount_tools
from safent_ads.mcp.presentation.registry import ToolRegistry
from safent_ads.shared.bearer import extract_bearer_token

logger = structlog.get_logger(__name__)

# La URL publicada del transporte (contracts/mcp.md, `AdsCredentialIssued.
# claude_code_command`/`codex_command`): `https://<host>/mcp`, SIN barra
# final. Los tres `MCPServer` registran su `Route` en este mismo path, y
# `composition/app.py` engancha el enrutador en el padre con este path
# exacto -- ambos leen esta constante para que no puedan separarse.
MCP_ENDPOINT_PATH = "/mcp"

_MAX_AUTHORIZATION_HEADER_CHARS = 8192

# M-4: tope de cuerpo HTTP en `/mcp` -- por encima del `content_base64`
# mas grande que aceptamos (`args.py::_MAX_CREATIVE_UPLOAD_BASE64_CHARS`,
# 12_000_000 caracteres) mas margen para el resto del sobre JSON-RPC.
_MAX_MCP_BODY_BYTES = 13_000_000

# Regla de no interferencia (004 tasks-2.md §0, Q3): la nota `instructions`
# del servidor SOLO dice el flujo, las reglas de aprobacion y la revision
# previa obligatoria de la historia 24 -- ni una linea sobre como pensar,
# redactar o decidir. El arnes recibe el catalogo COMPLETO que su permiso
# alcanza y decide por si mismo con su stack nativo.
#
# Imagen publica generica (lane 006-cloudflare-ui): la nota nunca lleva el
# nombre de un cliente concreto a pie de letra -- `build_mcp_instructions`
# la rellena con `ApiSettings.brand_name` (`ADS_BRAND_NAME`, "tu negocio"
# por defecto) y el host de `ADS_PUBLIC_BASE_URL`, nunca un nombre ni un
# host de cliente fijos. `MCP_INSTRUCTIONS` es el valor por defecto
# (imagen generica) que usan los tests y las llamadas que no pasan
# `instructions=` a `build_mcp_servers`.
_DEFAULT_BRAND_NAME = "tu negocio"
_DEFAULT_PANEL_HOST = "tu dominio"
_MCP_INSTRUCTIONS_TEMPLATE = """\
Sistema de anuncios de {brand_name}. Contexto compartido: list_workspaces ->
get_workspace; guarda cambios con propose_workspace/propose_workspace_campaign.
propose_workspace_creation crea una propuesta PAUSED, no una campaña remota.
La aprobación humana del diff autoriza su ejecución; verifica propuesta y recibo.
No confundas presupuesto planificado con límite aplicado ni creación con activación.
Panel, Codex y Claude comparten estado; no reconstruyas el proyecto desde el chat.
Encargos del panel: list_runtime_jobs -> claim_runtime_job -> heartbeat_runtime_job
-> propose_runtime_result. Sólo preparación; no autoriza gasto. Un conector activo
es necesario para recibir encargos sin un mensaje del usuario.
Flujo: cuentas conectadas -> oferta o brief -> borrador o propuesta -> aprobación
humana en el panel ({panel_host}) -> verificación.
1. Antes de proponer nada, mira qué hay: `list_businesses`, `list_platform_accounts`,
   `get_portfolio_overview`, `list_offerings`.
2. Si falta una cuenta de Meta o Google, usa `connect_platform_account` (sólo permiso
   aprobar) y pide a la persona que abra el enlace; confirma con `get_connection_status`.
3. Cualquier `propose_*`, `withdraw_proposal`, `generate_*`, `apply_defensive_action`,
   `create_offering`, `upload_creative_asset` o `connect_platform_account` crea una
   propuesta o un activo pendiente: nunca publica, gasta ni cambia nada por sí sola.
4. Esas herramientas llevan `_meta["anthropic/requiresUserInteraction"]`: tu arnés pide
   confirmación antes de llamarlas.
5. La aprobación editorial de un lanzamiento encola preparación para el runtime.
   La aprobación operativa es distinta y conserva los controles de ejecución existentes.
6. Después de una aprobación, verifica con `get_proposal` o `search_decision_log`.
7. REVISIÓN PREVIA OBLIGATORIA antes de proponer una campaña o un paquete:
   (a) `search_competitor_ads` para los anuncios recientes de la competencia en el país;
   (b) `list_top_performing_ads` para los anuncios propios con mejor resultado en 30-90 días;
   (c) el `cause.text` de la propuesta cita ambos, con enlaces y cifras. Si
       `search_competitor_ads` devuelve `available: false`, enseña `next_steps` y los enlaces.
   Una propuesta sin (a), (b) y (c) está incompleta.
8. Elige el canal de Google por el objetivo (`advertising_channel_type` en
   `propose_campaign_draft`/`propose_campaign_package`): reservas o clientes potenciales
   -> SEARCH; con conversiones verificadas, imágenes y señales de audiencia, súmale
   PERFORMANCE_MAX. Darse a conocer o alcance local -> DEMAND_GEN. Recordar o completar
   -> DISPLAY. `list_google_conversion_actions` antes de PERFORMANCE_MAX o DEMAND_GEN.
   MANUAL_CPC sólo en SEARCH/DISPLAY; el resto puja por conversión con expansión de URL
   y automatización de texto siempre apagadas. Toda campaña nace PAUSED: la publica sólo
   la aprobación humana del paquete.
Nunca inventes IDs de negocio, entidad o propuesta."""


def build_mcp_instructions(*, brand_name: str, panel_url: str) -> str:
    """`composition/app.py` llama esto con `ApiSettings.brand_name`/
    `public_base_url` reales; el host se deriva de la URL completa
    (`ADS_PUBLIC_BASE_URL`, nunca un literal aparte que pueda divergir)."""
    panel_host = urlsplit(panel_url).netloc or panel_url
    return _MCP_INSTRUCTIONS_TEMPLATE.format(brand_name=brand_name, panel_host=panel_host)


# Imagen generica por defecto: lo que usan los tests y cualquier llamada a
# `build_mcp_servers` que no pase `instructions=` explicitamente.
MCP_INSTRUCTIONS = build_mcp_instructions(
    brand_name=_DEFAULT_BRAND_NAME, panel_url=_DEFAULT_PANEL_HOST
)


def _error_response(status_code: int, *, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def unauthorized_response(*, resource_metadata_url: str) -> JSONResponse:
    """Mismo cuerpo y cabecera `WWW-Authenticate` que
    `RequireAuthMiddleware._send_auth_error`
    (`mcp/server/auth/middleware/bearer_auth.py`) -- para que `/mcp` y
    `GET /mcp/health` (`mcp/presentation/health.py`, C-48) sean
    indistinguibles ante un cliente sin autenticar (RFC 9728 SS5.1) y un
    cliente MCP pueda descubrir el AS y arrancar el flujo OAuth."""
    www_authenticate = (
        'Bearer error="invalid_token", error_description="Authentication required", '
        f'resource_metadata="{resource_metadata_url}"'
    )
    return JSONResponse(
        {"error": "invalid_token", "error_description": "Authentication required"},
        status_code=401,
        headers={"WWW-Authenticate": www_authenticate, "Cache-Control": "no-store"},
    )


def _unauthorized_response(resource_metadata_url: str | None = None) -> JSONResponse:
    """Sin `resource_metadata_url` (llamadas internas de servicio fuera de
    `/mcp`) se mantiene el sobre `{"error": {...}}` del resto de la
    superficie; con el, el 401 de RFC 9728 que espera un cliente MCP."""
    if resource_metadata_url is None:
        return _error_response(401, code="UNAUTHORIZED", message="Token invalido o ausente.")
    return unauthorized_response(resource_metadata_url=resource_metadata_url)


def _seat_authority_unavailable_response() -> JSONResponse:
    return _error_response(
        503,
        code="SEAT_AUTHORITY_UNAVAILABLE",
        message="La autorizacion de anuncios no esta disponible ahora mismo.",
    )


def _forbidden_response() -> JSONResponse:
    return _error_response(403, code="FORBIDDEN", message="Origen no permitido.")


def _payload_too_large_response() -> JSONResponse:
    return _error_response(
        413, code="PAYLOAD_TOO_LARGE", message="El cuerpo de la peticion supera el tope permitido."
    )


class _StreamedBodyTooLarge(Exception):
    """Interna a `ContentLengthLimitMiddleware` (R-3): corta la lectura del
    cuerpo en marcha en cuanto el recuento real de bytes supera el tope,
    sin importar lo que diga (o calle) `Content-Length`."""


class ContentLengthLimitMiddleware:
    """ASGI delante de `/mcp` (M-4, R-3): un `Content-Length` declarado por
    encima del tope (o ilegible) se rechaza con `413` ANTES de que el SDK
    MCP lea o parsee el cuerpo. Sin cabecera fiable -- `Transfer-Encoding:
    chunked` o un `Content-Length` que miente por debajo del tamano real --
    el recuento se hace en marcha envolviendo `receive`: cada mensaje
    `http.request` suma su `body` al total, y en cuanto supera el tope se
    aborta la lectura y se responde `413` sin dejar que seguir leyendo
    bytes sin cota. Sin cabecera y sin cuerpo (streamable-http usa tambien
    `GET`/`DELETE`) no hay nada que contar, se deja pasar."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int = _MAX_MCP_BODY_BYTES) -> None:
        self._app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if self._declared_length_too_large(scope):
            logger.warning("mcp_payload_too_large", path=Request(scope, receive=receive).url.path)
            await _payload_too_large_response()(scope, receive, send)
            return
        try:
            await self._app(scope, self._capped_receive(receive), send)
        except _StreamedBodyTooLarge:
            logger.warning(
                "mcp_payload_too_large_streamed",
                path=Request(scope, receive=receive).url.path,
            )
            await _payload_too_large_response()(scope, receive, send)

    def _capped_receive(self, receive: Receive) -> Receive:
        bytes_seen = 0

        async def wrapped() -> Message:
            nonlocal bytes_seen
            message = await receive()
            if message.get("type") == "http.request":
                bytes_seen += len(message.get("body") or b"")
                if bytes_seen > self._max_body_bytes:
                    raise _StreamedBodyTooLarge
            return message

        return wrapped

    def _declared_length_too_large(self, scope: Scope) -> bool:
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    return int(value) > self._max_body_bytes
                except ValueError:
                    return True
        return False


def _introspection_rate_limited_response(retry_after_seconds: int) -> JSONResponse:
    """contracts/mcp.md §6: cupo de introspecciones agotado -- `429` con
    `Retry-After`, nunca un alcance por defecto."""
    response = _error_response(
        429,
        code="RATE_LIMITED",
        message="Cupo de introspecciones agotado, reintenta en breve.",
    )
    response.headers["retry-after"] = str(retry_after_seconds)
    return response


def build_mcp_servers(
    *,
    registries: Mapping[Permission, ToolRegistry],
    dispatcher: ToolDispatcher,
    instructions: str = MCP_INSTRUCTIONS,
) -> dict[Permission, MCPServer]:
    """Un `MCPServer` por permiso (T044/A6), mismo `ToolDispatcher` y mismo
    `Container` para los tres -- coste: tres tablas de esquemas en
    memoria; beneficio: omision real de herramientas sin tocar el SDK.

    `known_tool_names` (bug 1, hotfix 0.2.20): la union de las tres listas
    filtradas, para que `mount_tools` distinga un nombre del catalogo
    completo que ESTE permiso no monta (denegado y auditado por el
    `ToolDispatcher`) de un nombre que el cliente MCP se inventa (INV-2,
    "unknown tool" generico del SDK, sin fila de auditoria)."""
    known_tool_names = frozenset(
        definition.name for registry in registries.values() for definition in registry
    )
    return {
        permission: mount_tools(
            MCPServer(
                name=f"ads-{permission.value}", version=__version__, instructions=instructions
            ),
            registry=registry,
            dispatcher=dispatcher,
            known_tool_names=known_tool_names,
        )
        for permission, registry in registries.items()
    }


class SeatCredentialRouter:
    """ASGI delante de la ruta exacta `/mcp`: resuelve la credencial de
    puesto UNA vez por peticion contra Enterprise y delega en el sub-app
    del permiso resuelto. `Host`/`Origin` ajenos se rechazan antes de tocar
    Enterprise (protegen contra DNS-rebinding sin gastar una introspeccion)."""

    def __init__(
        self,
        apps: Mapping[Permission, ASGIApp],
        *,
        caller_scope_resolver: CallerScopeResolverPort,
        allowed_hosts: frozenset[str],
        resource_metadata_url: str | None = None,
    ) -> None:
        self._apps = apps
        self._resolver = caller_scope_resolver
        self._allowed_hosts = allowed_hosts
        self._resource_metadata_url = resource_metadata_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Nunca deberia llegar aqui: `Route(..., endpoint=router)` solo
            # despacha peticiones HTTP (`composition/app.py::_route_mcp_transport`).
            return
        request = Request(scope, receive=receive)
        if not self._host_allowed(request):
            await _forbidden_response()(scope, receive, send)
            return
        token = self._bearer(request)
        if token is None:
            logger.warning("mcp_unauthorized_attempt", path=request.url.path)
            await self._unauthorized()(scope, receive, send)
            return
        try:
            caller_scope = await self._resolver.resolve(token)
        except SeatAuthorityDeniedError:
            logger.warning("mcp_unauthorized_attempt", path=request.url.path)
            await self._unauthorized()(scope, receive, send)
            return
        except SeatAuthorityUnavailableError:
            logger.warning("mcp_seat_authority_unavailable", path=request.url.path)
            await _seat_authority_unavailable_response()(scope, receive, send)
            return
        except IntrospectionQuotaExceededError as exc:
            logger.warning("mcp_introspection_rate_limited", path=request.url.path)
            response = _introspection_rate_limited_response(exc.retry_after_seconds)
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})[CALLER_SCOPE_STATE_ATTR] = caller_scope
        await self._apps[caller_scope.permission](scope, receive, send)

    def _unauthorized(self) -> JSONResponse:
        return _unauthorized_response(self._resource_metadata_url)

    def _bearer(self, request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        if len(header) > _MAX_AUTHORIZATION_HEADER_CHARS:
            return None
        return extract_bearer_token(header)

    def _host_allowed(self, request: Request) -> bool:
        host = request.headers.get("host", "").split(":", 1)[0]
        return host in self._allowed_hosts


def _transport_security_for(
    public_base_url: str, *, extra_allowed_hosts: frozenset[str] = frozenset()
) -> TransportSecuritySettings:
    """Restringe `Host`/`Origin` al dominio publico real -- proteccion anti
    DNS-rebinding, aplicada tambien dentro de cada sub-app (defensa en
    profundidad junto a `SeatCredentialRouter._host_allowed`).

    `extra_allowed_hosts` (`ADS_MCP_EXTRA_ALLOWED_HOSTS`, settings.py):
    hostnames provisionales servidos por el MISMO Caddy que
    `public_base_url` (p.ej. un `sslip.io` antes de que el DNS propio
    apunte) -- siempre como origen `https://`, nunca `http://`, igual que
    el dominio principal."""
    host = urlsplit(public_base_url).netloc
    extra_hosts = sorted(extra_allowed_hosts)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host, "127.0.0.1", "localhost", *extra_hosts],
        allowed_origins=[
            public_base_url,
            "https://127.0.0.1",
            "http://127.0.0.1",
            *(f"https://{extra_host}" for extra_host in extra_hosts),
        ],
    )


def _allowed_hosts_for(
    public_base_url: str, *, extra_allowed_hosts: frozenset[str] = frozenset()
) -> frozenset[str]:
    host = urlsplit(public_base_url).netloc.split(":", 1)[0]
    return frozenset({host, "127.0.0.1", "localhost"}) | extra_allowed_hosts


def build_mcp_asgi_apps(
    mcp_servers: Mapping[Permission, MCPServer],
    *,
    caller_scope_resolver: CallerScopeResolverPort,
    public_base_url: str,
    extra_allowed_hosts: frozenset[str] = frozenset(),
    resource_metadata_url: str | None = None,
) -> tuple[SeatCredentialRouter, dict[Permission, Starlette]]:
    """Construye los tres sub-apps streamable-http (`stateless_http=True,
    json_response=True`, contracts/mcp.md) y el enrutador que los precede.
    Devuelve tambien los sub-apps para que `composition/app.py` entre en el
    `lifespan_context` de cada uno (Starlette no reenvia el evento ASGI
    `lifespan` a una sub-app montada como `Route`)."""
    security = _transport_security_for(public_base_url, extra_allowed_hosts=extra_allowed_hosts)
    apps = {
        permission: server.streamable_http_app(
            streamable_http_path=MCP_ENDPOINT_PATH,
            stateless_http=True,
            json_response=True,
            transport_security=security,
        )
        for permission, server in mcp_servers.items()
    }
    router = SeatCredentialRouter(
        apps,
        caller_scope_resolver=caller_scope_resolver,
        allowed_hosts=_allowed_hosts_for(public_base_url, extra_allowed_hosts=extra_allowed_hosts),
        resource_metadata_url=resource_metadata_url,
    )
    return router, apps
