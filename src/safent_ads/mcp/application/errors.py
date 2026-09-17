"""Errores tipados del dispatch MCP (contracts/mcp-tools.md §Reglas del
contrato regla 6). `ToolDispatcher` los lanza; `presentation/mount.py` los
traduce al borde del SDK (shared/errors.py: "excepciones de dominio ->
mapeador en presentacion")."""

from __future__ import annotations

from safent_ads.shared.errors import ApplicationError


class ToolDispatchError(ApplicationError):
    """Raiz de los errores tipados del contrato MCP. `code` es el
    identificador estable que ve el cliente (contracts/mcp-tools.md
    §Reglas del contrato)."""

    code: str = "UNKNOWN"


class ToolNotAllowedError(ToolDispatchError):
    """INV-2: el nombre no esta en `ToolRegistry`, sin importar lo que el
    modelo crea poder llamar (threat-model.md C-2)."""

    code = "TOOL_NOT_ALLOWED"


class BusinessForbiddenError(ToolDispatchError):
    """La `CallerScope` del bearer no alcanza a este `business_id`
    (contracts/mcp-tools.md regla 4)."""

    code = "BUSINESS_FORBIDDEN"


class EntityNotFoundError(ToolDispatchError):
    """El identificador no existe, o existe en otro negocio: mismo codigo
    para ambos casos, para no filtrar existencia entre negocios (mismo
    principio IDOR-safe que `contracts/rest-api.md`)."""

    code = "ENTITY_NOT_FOUND"


class ToolValidationError(ToolDispatchError):
    """Los argumentos no validan contra el modelo pydantic estricto de la
    herramienta (threat-model.md C-11)."""

    code = "VALIDATION_ERROR"


class RateLimitedError(ToolDispatchError):
    """Cuota por minuto agotada para este llamador (threat-model.md C-23)."""

    code = "RATE_LIMITED"


class ForbiddenScopeError(ToolDispatchError):
    """El token presentado no concedio el alcance que exige
    `ToolClass.PROPOSAL` (spec 002 tasks.md T012): `ads:read` no basta
    para crear una propuesta, aunque el negocio si sea accesible."""

    code = "FORBIDDEN_SCOPE"


class NativeAdsUnavailableError(ToolDispatchError):
    """El conector MCP oficial no admite esta lectura; no describe Composio."""

    code = "NATIVE_MCP_UNAVAILABLE"


class PlatformAppNotConfiguredError(ToolDispatchError):
    """El broker denego con `PLATFORM_APP_NOT_CONFIGURED`: la cuenta esta
    conectada, pero este servidor no tiene la app de plataforma (Meta/Google)
    ni Composio configurados (incidente de produccion, companion 0.2.21:
    `list_meta_pages`/`list_google_conversion_actions` filtraban esto como
    `UnexpectedToolError` en vez de este sobre limpio)."""

    code = "PLATFORM_APP_NOT_CONFIGURED"


class CredentialNotConnectedError(ToolDispatchError):
    """El broker denego con `CREDENTIAL_NOT_CONNECTED`: la credencial de la
    cuenta no esta conectada o el proveedor la rechazo."""

    code = "CREDENTIAL_NOT_CONNECTED"


class PlatformUnavailableError(ToolDispatchError):
    """El broker denego con `PLATFORM_UNAVAILABLE`: Meta o Google no
    respondieron a esta lectura ahora mismo."""

    code = "PLATFORM_UNAVAILABLE"


class CapabilityNotAvailableError(ToolDispatchError):
    """El broker denego con `PLATFORM_CAPABILITY_NOT_IMPLEMENTED`: la
    plataforma no ofrece esta lectura para el tipo de cuenta conectada
    (p. ej. los catalogos de Meta cuelgan del Business, no de la cuenta)."""

    code = "PLATFORM_CAPABILITY_NOT_IMPLEMENTED"


class BrokerUnavailableError(ToolDispatchError):
    """El bróker (`$ADS_BROKER_SOCKET`) no respondio: fallo de transporte
    (`BrokerConnectionError`), nunca un error de dominio de la plataforma."""

    code = "BROKER_UNAVAILABLE"


# --- traduccion de `BrokerRequestDeniedError`/`BrokerConnectionError` en el
# borde de infraestructura (`mcp/infrastructure/broker_*.py`): un mapeo
# UNICO aqui, nunca copiado en cada puerto (mismo criterio que el guard
# anti-SSRF compartido) -- `error_code` es un `str` plano, nunca el tipo de
# `accounts.application.errors.BrokerRequestDeniedError`, para que esta capa
# de aplicacion no dependa de un tipo de infraestructura de otro contexto. ---

_PLATFORM_APP_NOT_CONFIGURED_MESSAGE = (
    "Cuenta conectada pero sin app de plataforma ni Composio configurados en "
    "este servidor; pide al dueño configurarlo en Ajustes → Tus cuentas"
)
_CREDENTIAL_NOT_CONNECTED_MESSAGE = (
    "La credencial de esta cuenta no esta conectada o el proveedor la "
    "rechazo; pide al dueño reconectarla en Ajustes → Tus cuentas"
)
_CAPABILITY_NOT_AVAILABLE_MESSAGE = (
    "Esta lectura no esta disponible en esta instalacion o para el tipo de "
    "cuenta conectada; no es un fallo temporal ni de credencial"
)
_PLATFORM_UNAVAILABLE_MESSAGE = (
    "La plataforma (Meta o Google) no respondio a esta lectura ahora mismo; "
    "vuelve a intentarlo en unos minutos"
)
_BROKER_UNAVAILABLE_MESSAGE = (
    "El servicio que habla con las plataformas no esta disponible ahora "
    "mismo; vuelve a intentarlo en unos minutos"
)
_BROKER_RATE_LIMITED_MESSAGE = (
    "La plataforma limito esta lectura por volumen de peticiones; vuelve a "
    "intentarlo en unos minutos"
)

_BROKER_DENIAL_FACTORIES: dict[str, type[ToolDispatchError]] = {
    "PLATFORM_APP_NOT_CONFIGURED": PlatformAppNotConfiguredError,
    "CREDENTIAL_NOT_CONNECTED": CredentialNotConnectedError,
    "PLATFORM_UNAVAILABLE": PlatformUnavailableError,
    "RATE_LIMITED": RateLimitedError,
    "PLATFORM_CAPABILITY_NOT_IMPLEMENTED": CapabilityNotAvailableError,
}
_BROKER_DENIAL_MESSAGES: dict[type[ToolDispatchError], str] = {
    PlatformAppNotConfiguredError: _PLATFORM_APP_NOT_CONFIGURED_MESSAGE,
    CredentialNotConnectedError: _CREDENTIAL_NOT_CONNECTED_MESSAGE,
    PlatformUnavailableError: _PLATFORM_UNAVAILABLE_MESSAGE,
    RateLimitedError: _BROKER_RATE_LIMITED_MESSAGE,
    CapabilityNotAvailableError: _CAPABILITY_NOT_AVAILABLE_MESSAGE,
}


def broker_denial_error(error_code: str) -> ToolDispatchError | None:
    """Traduce el `error_code` de un `BrokerRequestDeniedError` (bróker
    `broker_op_denied`) a un `ToolDispatchError` tipado y accionable, nunca
    el texto crudo del proveedor. `None` si el codigo no tiene traduccion
    conocida: quien llama debe relanzar la excepcion original tal cual --
    la vuelve `TOOL_FAILED` la red de seguridad generica de `mount.py`."""
    error_cls = _BROKER_DENIAL_FACTORIES.get(error_code)
    if error_cls is None:
        return None
    return error_cls(_BROKER_DENIAL_MESSAGES[error_cls])


def broker_unavailable_error() -> BrokerUnavailableError:
    """Traduce un `BrokerConnectionError` (transporte: socket, timeout,
    trama) a `BROKER_UNAVAILABLE` -- nunca el detalle de infraestructura."""
    return BrokerUnavailableError(_BROKER_UNAVAILABLE_MESSAGE)


# --- escrituras (US2/US3): codigos que solo produce `apply_defensive_action`
# y, mas adelante, el resto de `propose_*` (contracts/mcp-tools.md regla 6) ---


class StaleDataError(ToolDispatchError):
    """Datos obsoletos: ninguna decision autonoma se apoya en metricas
    viejas (`STALE_DATA`)."""

    code = "STALE_DATA"


class GuardrailBlockedError(ToolDispatchError):
    """El veredicto de guardarrailes en vivo rechaza la accion
    (`GUARDRAIL_BLOCKED`)."""

    code = "GUARDRAIL_BLOCKED"


class RuleNotApplicableError(ToolDispatchError):
    """La regla no existe, no esta `AUTO`/`enabled`, o su condicion no esta
    disparando ahora mismo (`RULE_NOT_APPLICABLE`)."""

    code = "RULE_NOT_APPLICABLE"


class BrakeEngagedError(ToolDispatchError):
    """El freno de emergencia bloquea esta ruta (`BRAKE_ENGAGED`)."""

    code = "BRAKE_ENGAGED"


class CreativeRenderQuotaExceededError(ToolDispatchError):
    """Cuota de renderizado de este negocio agotada (M-2, revision 0.2.22):
    `ads-broker` la aplica antes de llamar al proveedor."""

    code = "RENDER_QUOTA_EXCEEDED"


class CreativeRenderBudgetExceededError(ToolDispatchError):
    """Tope de coste por llamada superado (M-2): el trabajo no se encarece
    en silencio; el llamador debe bajar variantes o subir `max_cost`."""

    code = "RENDER_COST_CAP"


class CreativeRendererUnavailableError(ToolDispatchError):
    """La cascada de `RendererSelector` se agoto sin producir un activo
    (mismo `CREATIVE_RENDERER_UNAVAILABLE` que `rest-api.md` 409): capacidad
    reportada, nunca fingida (correccion del propietario 2026-09-09)."""

    code = "CREATIVE_RENDERER_UNAVAILABLE"
