"""Monta el `ToolRegistry`/`ToolDispatcher` (T044) sobre el `MCPServer` ya
construido en `presentation/http.py`. Un envoltorio por herramienta,
generado dinamicamente para que el SDK introspeccione el modelo de
argumentos exacto de cada una (schemas distintos por herramienta, no un
`dict` generico) y ceda el paso siempre al `ToolDispatcher` — nunca se
llama a un caso de uso directamente desde aqui (INV-2).

El `CallerScope` ya no se resuelve aqui (004 tasks.md A6): lo resuelve UNA
vez por peticion `SeatCredentialRouter` (`presentation/http.py`) y lo deja
en `request.state`, porque es el mismo `CallerScope` el que decide a que
de los tres `MCPServer` (uno por permiso) se enruta la peticion.

`_gate_call_tool` ya envolvia `MCPServer.call_tool` para bug 1 (denegar y
auditar un nombre del catalogo completo que este permiso no monta). Ahora
(bug 3, hotfix 0.2.20) el mismo cierre traduce tambien un `ValidationError`
de pydantic que el SDK valida ANTES de que el cuerpo del wrapper se
ejecute -- hoy lo relanzaba como `ToolError` con el volcado crudo de
pydantic (`pydantic.dev`, `input_value=...`) como mensaje, sin pasar por
el sobre limpio del resto de herramientas.

Red de seguridad generica (incidente de produccion, companion 0.2.21):
un `BrokerRequestDeniedError`/excepcion cualquiera que un handler deja
escapar sin traducir a `ToolDispatchError` (`_build_wrapper` solo atrapa
ese tipo) llega al SDK como un crash de verdad -- `UnexpectedToolError`,
"Error executing tool X" opaco para el modelo, `__cause__` con el fallo
real. `_gate_call_tool` lo atrapa aqui, registra el traceback SOLO en el
log del servidor (`logger.exception`, nunca en la respuesta) y devuelve el
mismo sobre `{"error": {...}}` con el codigo generico `TOOL_FAILED` -- la
llamada ya quedo auditada como `outcome="error"` en `ToolDispatcher.
dispatch` antes de que la excepcion llegara hasta aqui."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Final

import structlog
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp_types import CallToolResult, InputRequiredResult, TextContent
from pydantic import ValidationError

from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.application.errors import ToolDispatchError
from safent_ads.mcp.presentation.dispatcher import ToolDispatcher
from safent_ads.mcp.presentation.registry import ToolClass, ToolRegistry

logger = structlog.get_logger(__name__)

_TOOL_FAILED_MESSAGE = "La herramienta fallo en el servidor; el equipo de Safent tiene el detalle."

# Clave en `request.state`/`scope["state"]` donde `SeatCredentialRouter`
# deja el `CallerScope` ya resuelto (una introspeccion por peticion HTTP,
# nunca una por herramienta -- contracts/mcp.md §2).
CALLER_SCOPE_STATE_ATTR: Final = "safent_ads_caller_scope"

# contracts/mcp.md §3.4: toda herramienta de estas cuatro clases decide o
# cambia algo (propuesta, escritura de catalogo, conexion de plataforma o
# activo creativo) -- nunca una `READ`. El arnes (Claude Code/Codex) fuerza
# su tarjeta de confirmacion nativa antes de llamarlas; ninguna regla `AUTO`
# puede saltarsela.
_TOOL_CLASSES_REQUIRING_USER_INTERACTION: Final = frozenset(
    {
        ToolClass.PROPOSAL,
        ToolClass.CATALOG_WRITE,
        ToolClass.CONNECTION_WRITE,
        ToolClass.CREATIVE_WRITE,
    }
)
_REQUIRES_USER_INTERACTION_META: Final[dict[str, Any]] = {
    "anthropic/requiresUserInteraction": True
}


def mount_tools(
    server: MCPServer,
    *,
    registry: ToolRegistry,
    dispatcher: ToolDispatcher,
    known_tool_names: frozenset[str] = frozenset(),
) -> MCPServer:
    mounted_names: set[str] = set()
    for definition in registry:
        wrapper = _build_wrapper(definition.name, definition.args_model, dispatcher)
        server.add_tool(
            wrapper,
            name=definition.name,
            description=definition.description,
            meta=_native_confirmation_meta(definition.tool_class),
        )
        mounted_names.add(definition.name)
    denied_names = frozenset(known_tool_names - mounted_names)
    _gate_call_tool(server, dispatcher=dispatcher, denied_names=denied_names)
    return server


def _native_confirmation_meta(tool_class: ToolClass) -> dict[str, Any] | None:
    if tool_class in _TOOL_CLASSES_REQUIRING_USER_INTERACTION:
        return _REQUIRES_USER_INTERACTION_META
    return None


def _gate_call_tool(
    server: MCPServer, *, dispatcher: ToolDispatcher, denied_names: frozenset[str]
) -> None:
    """Sustituye la referencia de INSTANCIA a `call_tool`: `_handle_call_tool`
    (SDK) resuelve `self.call_tool` en cada peticion, asi que este cierre lo
    ve incluso sin subclasificar `MCPServer`."""
    original_call_tool = server.call_tool

    async def call_tool(
        name: str, arguments: dict[str, Any], context: Context | None = None
    ) -> CallToolResult | InputRequiredResult:
        if name in denied_names:
            return await _deny(name, arguments, context, dispatcher=dispatcher)
        try:
            return await original_call_tool(name, arguments, context)
        except ToolError as exc:
            # M-5: `UnexpectedToolError` (subclase de `ToolError`) envuelve un
            # CRASH -- el handler o algo mas abajo, no la validacion de
            # entrada del SDK. Se comprueba PRIMERO: su `__cause__` puede ser
            # un `ValidationError` interno (p.ej. el de conversion de la
            # respuesta) sin relacion con lo que mando el llamante, y ese
            # caso nunca debe traducirse a `_invalid_arguments_result`
            # (expondria rutas de modelos internos) sino al sobre generico.
            if isinstance(exc, UnexpectedToolError):
                return _tool_failed_result(name, exc)
            # Unico `ToolError` "normal" del SDK que se traduce: fallo de
            # esquema de argumentos (nunca subclase, ya descartado arriba).
            if isinstance(exc.__cause__, ValidationError):
                return _invalid_arguments_result(exc.__cause__)
            raise

    server.call_tool = call_tool  # type: ignore[method-assign]


async def _deny(
    name: str,
    arguments: dict[str, Any],
    context: Context | None,
    *,
    dispatcher: ToolDispatcher,
) -> CallToolResult:
    caller_scope = _caller_scope_from_context(context)
    envelope = await dispatcher.deny(name, arguments, caller_scope=caller_scope)
    return _envelope_result(envelope)


def _invalid_arguments_result(exc: ValidationError) -> CallToolResult:
    """Bug 3 (hotfix 0.2.20): mismo sobre `{"error": {...}}` que cualquier
    `ToolDispatchError`, nunca el volcado crudo de pydantic (URLs
    `pydantic.dev`, `input_value=...`) que el SDK produce por defecto."""
    fields = [
        {
            "path": ".".join(str(part) for part in error["loc"][1:] or error["loc"]),
            "message": error["msg"],
        }
        for error in exc.errors()
    ]
    return _envelope_result(
        {
            "error": {
                "code": "INVALID_ARGUMENTS",
                "message": "Los argumentos no son validos.",
                "fields": fields,
            }
        }
    )


def _tool_failed_result(tool_name: str, exc: UnexpectedToolError) -> CallToolResult:
    """Incidente de produccion (companion 0.2.21): el sobre `TOOL_FAILED`
    generico para cualquier crash que no llego ya tipado como
    `ToolDispatchError`. `logger.exception` (llamado dentro del `except`
    que sigue en curso) registra el traceback real SOLO en el log del
    servidor -- nunca en la respuesta que ve el modelo -- con la misma
    redaccion de `logging_setup.py` que cualquier otro crash del proceso."""
    logger.exception("mcp_tool_crashed", tool=tool_name, cause=type(exc.__cause__).__name__)
    return _envelope_result({"error": {"code": "TOOL_FAILED", "message": _TOOL_FAILED_MESSAGE}})


def _envelope_result(payload: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structured_content=payload,
    )


def _build_wrapper(
    tool_name: str, args_model: type[Any], dispatcher: ToolDispatcher
) -> Callable[..., Awaitable[dict[str, Any]]]:
    async def wrapper(args: Any, ctx: Context) -> dict[str, Any]:
        caller_scope = _caller_scope_from_context(ctx)
        # Preserve PATCH semantics through the SDK's first validation pass.
        # Omitted fields are not explicit nulls (including nested draft fields).
        raw_arguments = args.model_dump(mode="json", by_alias=True, exclude_unset=True)
        try:
            return await dispatcher.dispatch(tool_name, raw_arguments, caller_scope=caller_scope)
        except ToolDispatchError as exc:
            return {"error": {"code": exc.code, "message": str(exc)}}

    # `from __future__ import annotations` deja el resto del modulo con
    # anotaciones como texto; el SDK necesita el tipo real para construir
    # el schema de cada herramienta, asi que se fija explicitamente aqui.
    wrapper.__annotations__ = {"args": args_model, "ctx": Context, "return": dict[str, Any]}
    wrapper.__name__ = tool_name
    return wrapper


def _caller_scope_from_context(ctx: Context | None) -> CallerScope:
    """`SeatCredentialRouter` garantiza esta clave antes de delegar en el
    sub-app del permiso resuelto: si falta, es un error de cableado, no una
    entrada de un cliente (`RuntimeError`, no un `ToolDispatchError`)."""
    if ctx is None:
        raise RuntimeError("CallerScope no resuelto: SeatCredentialRouter no se ejecuto antes")
    try:
        request = ctx.request_context.request
    except (ValueError, AttributeError):
        request = None
    caller_scope = getattr(request.state, CALLER_SCOPE_STATE_ATTR, None) if request else None
    if not isinstance(caller_scope, CallerScope):
        raise RuntimeError("CallerScope no resuelto: SeatCredentialRouter no se ejecuto antes")
    return caller_scope
