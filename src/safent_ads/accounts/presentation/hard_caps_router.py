"""`GET/PUT/DELETE /api/v1/accounts/{platform_account_id}/hard-caps`
(spec 008 T032, `contracts/hard-caps.openapi.yaml` v1.1.0).

Las tres capas de autorizacion, en este orden y no en otro:

1. **Sesion de panel por cookie** (`CURRENT_OWNER`). No hay via bearer: un
   `Authorization: Bearer ...` de agente o de cuenta de servicio, sin
   cookie, recibe 401 por construccion -- `current_owner` solo mira la
   cookie. Ninguna herramienta MCP expone esta superficie.
2. **CSRF** por doble envio. `CsrfMiddleware` ya corta toda mutacion del
   panel antes de llegar aqui, y `require_action_confirmation` lo vuelve a
   comprobar: las dos capas conviven, ninguna sustituye a la otra.
3. **Re-identificacion fresca** cuando el cambio SUBE (definicion normativa
   en `accounts/application/hard_caps.py`) y **confirmacion de accion de un
   solo uso** siempre en PUT y DELETE. El 401 de frescura llega SIEMPRE
   antes que el 428 de confirmacion, igual que en `grants_router.py`: sin
   frescura, `require_action_confirmation` ni se invoca.

Honestidad sobre el alcance: la re-identificacion vive en `ads-api`, el
componente que el modelo de amenazas da por comprometido. Protege frente a
sesion robada, CSRF y XSS -- **no** frente a un `ads-api` comprometido. El
unico control frente a eso es el sobre del fichero, que esta capa no puede
tocar porque no es ella quien lo aplica.

Esta API no puede saltarse ninguna regla de topes, porque no aplica
ninguna: reenvia al broker y traduce su veredicto. Una cuenta sin tope
sigue denegando el 100 % de las escrituras y este router no puede crear una
excepcion a eso."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Annotated, Any, Final

import structlog
from fastapi import APIRouter, Body, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.application.hard_caps import (
    DeleteAccountHardCaps,
    GetAccountHardCaps,
    SetAccountHardCaps,
    relaxes_the_effective_cap,
    withdrawal_relaxes_the_effective_cap,
)
from safent_ads.accounts.application.hard_caps_ports import (
    AccountHardCapsView,
    CapAmountsView,
    HardCapsBrokerPort,
    PanelCapsInput,
    PlatformAccountDirectoryPort,
)
from safent_ads.accounts.infrastructure.broker_hard_caps_client import BrokerHardCapsSocketClient
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.accounts.infrastructure.sql_platform_account_directory import (
    SqlPlatformAccountDirectory,
)
from safent_ads.audit.application.record_decision import RecordDecision
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.composition.container import Container
from safent_ads.composition.settings import ApiSettings
from safent_ads.iam.presentation.action_confirmation import require_action_confirmation
from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.iam.presentation.fresh_identification import require_fresh_identification
from safent_ads.shared.ids import BusinessId

logger = structlog.get_logger(__name__)

_ROUTER_PREFIX = "/api/v1/accounts"
_MAX_MINOR_AMOUNT: Final = 10**12
_DENIED_INVALID_SCHEMA: Final = "invalid_schema"

# Codigo del broker -> (estado HTTP, codigo de la API). Los cuatro del sobre
# son 409 (conflicto con una politica declarada, no un cuerpo mal formado),
# `INVALID_CAPS` es 400, el estado no escribible es 503 y `DENIED` es 404.
#
# `DENIED` es lo que responde `handle_payload` cuando la trama no pasa el
# esquema del socket -- el caso real es un id de mas de 64 caracteres, que el
# broker rechaza antes de mirar ninguna politica. Ese id no puede nombrar
# ninguna cuenta, asi que la respuesta honesta es la misma que para una
# cuenta que la sesion no alcanza: 404, nunca un 503 que culpa al broker de
# estar caido cuando esta perfectamente vivo.
_BROKER_DENIAL_STATUS: Final[dict[str, tuple[int, str]]] = {
    "DENIED": (404, "NOT_FOUND"),
    "ENVELOPE_NOT_DECLARED": (409, "ENVELOPE_NOT_DECLARED"),
    "ENVELOPE_EXCEEDED": (409, "ENVELOPE_EXCEEDED"),
    "ENVELOPE_ACCOUNTS_EXHAUSTED": (409, "ENVELOPE_ACCOUNTS_EXHAUSTED"),
    "ENVELOPE_CHANGES_EXHAUSTED": (409, "ENVELOPE_CHANGES_EXHAUSTED"),
    "INVALID_CAPS": (400, "INVALID_CAPS"),
    "CAPS_STATE_UNWRITABLE": (503, "CAPS_STATE_UNWRITABLE"),
}

_BROKER_DENIAL_MESSAGES: Final[dict[str, str]] = {
    "NOT_FOUND": "Cuenta no encontrada.",
    "ENVELOPE_NOT_DECLARED": (
        "Este despliegue no declara panel_managed en config/caps.yaml: "
        "los topes solo se fijan en ese fichero."
    ),
    "ENVELOPE_EXCEEDED": "El importe supera el sobre declarado en config/caps.yaml.",
    "ENVELOPE_ACCOUNTS_EXHAUSTED": (
        "Ya hay tantas cuentas con tope del panel como permite "
        "panel_managed.max_accounts en config/caps.yaml."
    ),
    "ENVELOPE_CHANGES_EXHAUSTED": (
        "Se agotaron los cambios de tope de hoy "
        "(panel_managed.max_cap_changes_per_day en config/caps.yaml)."
    ),
    "INVALID_CAPS": "Los topes pedidos no son validos.",
    "CAPS_STATE_UNWRITABLE": (
        "El broker no pudo escribir su directorio de topes "
        "(ADS_BROKER_CAPS_STATE_DIR). No se ha cambiado nada."
    ),
}


class HardCapsUpdateRequest(BaseModel):
    """Los TRES importes que el panel fija, mas la divisa de confirmacion.
    `extra="forbid"` mas `strict=True` es lo que hace que `floor_minor`,
    `max_step_pct`, `max_changes_per_day` y `autonomy_enabled` se rechacen
    ruidosamente (i14) y que `3.0`, `"3"` o `true` no pasen por un entero
    (i8)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    daily_cap_minor: int = Field(ge=0, le=_MAX_MINOR_AMOUNT)
    monthly_cap_minor: int = Field(ge=0, le=_MAX_MINOR_AMOUNT)
    ceiling_minor: int = Field(ge=0, le=_MAX_MINOR_AMOUNT)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


def _invalid_caps(message: str) -> ApiError:
    return ApiError(status_code=400, code="INVALID_CAPS", message=message)


def _not_found() -> ApiError:
    """404, nunca 403: la regla del repositorio para no filtrar existencia."""
    return ApiError(status_code=404, code="NOT_FOUND", message="Cuenta no encontrada.")


def _broker_unavailable() -> ApiError:
    return ApiError(
        status_code=503,
        code="BROKER_UNAVAILABLE",
        message="El broker de topes no responde. No se ha cambiado nada.",
    )


def _translate_denial(exc: BrokerRequestDeniedError) -> ApiError:
    """Un codigo que esta capa no conoce NO se deja pasar como exito ni se
    reinterpreta: fail-closed, y queda en el log con su codigo."""
    mapped = _BROKER_DENIAL_STATUS.get(exc.error_code)
    if mapped is None:
        logger.warning("hard_caps_broker_denial_unmapped", error_code=exc.error_code)
        return _broker_unavailable()
    # M-8 (revision T035): `DENIED` solo es "esa cuenta no existe" cuando el
    # motivo es `invalid_schema` (id que no puede nombrar ninguna cuenta). Un
    # `unauthorized_peer` (uid mal configurado) o un `op_not_available_in_f1`
    # (broker mas viejo que la API) es un despliegue roto: 503 con traza, nunca
    # un 404 que lo disfrace de "cuenta no encontrada".
    if exc.error_code == "DENIED" and exc.reason != _DENIED_INVALID_SCHEMA:
        logger.warning("hard_caps_broker_denied", reason=exc.reason)
        return _broker_unavailable()
    status_code, code = mapped
    return ApiError(status_code=status_code, code=code, message=_BROKER_DENIAL_MESSAGES[code])


def _parse_body(body: Any) -> PanelCapsInput:  # noqa: ANN401 - cuerpo crudo del cliente
    """El `json.loads` de Starlette acepta `NaN`/`Infinity` y aqui no se le
    puede pasar `parse_constant` (lo llama el framework). No hace falta: los
    tres importes son `int` en modo estricto, y un `float` -- `NaN` incluido
    -- no pasa por un entero estricto. La trama del socket del broker SI
    lleva `parse_constant`, porque alli el `json.loads` es nuestro y un
    `NaN` en un importe haria falsa toda comparacion contra el sobre."""
    if not isinstance(body, dict):
        raise _invalid_caps("El cuerpo debe ser un objeto JSON.")
    try:
        parsed = HardCapsUpdateRequest.model_validate(body)
    except ValidationError as exc:
        raise _invalid_caps(_first_problem(exc)) from exc
    return PanelCapsInput(
        daily_cap_minor=parsed.daily_cap_minor,
        monthly_cap_minor=parsed.monthly_cap_minor,
        ceiling_minor=parsed.ceiling_minor,
        currency=parsed.currency,
    )


def _first_problem(exc: ValidationError) -> str:
    """Nombra el campo que explica el rechazo, sin devolver el valor: un
    mensaje de error no es el sitio donde repetir lo que llego."""
    error = exc.errors()[0]
    field = ".".join(str(part) for part in error["loc"]) or "cuerpo"
    return f"Campo {field}: {error['msg']}."


def _action_hash(platform_account_id: str, caps: PanelCapsInput | None) -> str:
    """Incluye la cuenta Y los tres importes pedidos: una confirmacion de
    una subida a X no sirve para otra a Y > X dentro de la ventana de
    frescura."""
    if caps is None:
        material = f"account_hard_caps_delete|{platform_account_id}"
    else:
        material = (
            f"account_hard_caps_set|{platform_account_id}"
            f"|{caps.daily_cap_minor}|{caps.monthly_cap_minor}"
            f"|{caps.ceiling_minor}|{caps.currency}"
        )
    return hashlib.sha256(material.encode()).hexdigest()


def _response(view: AccountHardCapsView, *, currency: str) -> dict[str, Any]:
    return {
        "platform_account_id": view.platform_account_id,
        "source": view.source,
        "writable": view.writable,
        "currency": currency,
        "effective": _effective(view),
        "from_file": _cap_amounts(view.from_file),
        "from_panel": _cap_amounts(view.from_panel),
        "clamped_by": list(view.clamped_by),
        "panel_state_available": view.panel_state_available,
        "envelope": _envelope(view),
    }


def _effective(view: AccountHardCapsView) -> dict[str, int] | None:
    if view.effective is None:
        return None
    return {
        "daily_cap_minor": view.effective.daily_cap_minor,
        "monthly_cap_minor": view.effective.monthly_cap_minor,
        "floor_minor": view.effective.floor_minor,
        "ceiling_minor": view.effective.ceiling_minor,
    }


def _cap_amounts(amounts: CapAmountsView | None) -> dict[str, int] | None:
    """Lo GUARDADO a cada lado, que no siempre es lo que se aplica: con ello
    la pantalla dice «guardado X, en vigor Y» en un campo recortado, en vez
    de dejar al dueno adivinando cual de los dos esta viendo. Sin divisa
    dentro: la de la vista es una sola y ya viaja arriba."""
    if amounts is None:
        return None
    return {
        "daily_cap_minor": amounts.daily_cap_minor,
        "monthly_cap_minor": amounts.monthly_cap_minor,
        "ceiling_minor": amounts.ceiling_minor,
    }


def _envelope(view: AccountHardCapsView) -> dict[str, Any] | None:
    envelope = view.envelope
    if envelope is None:
        return None
    return {
        "max_daily_cap_minor": envelope.max_daily_cap_minor,
        "max_monthly_cap_minor": envelope.max_monthly_cap_minor,
        "max_ceiling_minor": envelope.max_ceiling_minor,
        "min_floor_minor": envelope.min_floor_minor,
        "max_accounts": envelope.max_accounts,
        "accounts_used": envelope.accounts_used,
        "max_cap_changes_per_day": envelope.max_cap_changes_per_day,
        "cap_changes_today": envelope.cap_changes_today,
        "currency": envelope.currency,
    }


# El motor convierte a unidad menor con un factor fijo (x100,
# `money_minor_units`) sin mirar la divisa (R-25); esta constante solo
# rellena la vista cuando no hay sobre y el cliente no declaro divisa. No
# participa en ninguna decision, y por eso se declara ANTES de su unico uso
# en vez de quedar suelta a mitad del modulo.
_ENGINE_DEFAULT_CURRENCY: Final = "EUR"


def _currency_of(view: AccountHardCapsView, fallback: str | None = None) -> str:
    """La divisa sale del sobre, nunca de un literal del panel. Sin sobre
    declarado no hay divisa que el panel pueda usar para fijar nada; se
    devuelve la que pidio el cliente, o la del motor si no pidio ninguna --
    nunca `null`, para que la vista siempre sepa con que formatear."""
    if view.envelope is not None:
        return view.envelope.currency
    return fallback or _ENGINE_DEFAULT_CURRENCY


# Como se alcanza el directorio de cuentas: el puerto es de la capa de
# aplicacion y el adaptador SQL se inyecta aqui, en el unico sitio que ya
# conoce la infraestructura. Asi el router depende de la abstraccion, y un
# test puede darle un directorio en memoria sin levantar Postgres.
type _DirectoryFactory = Callable[[AsyncSession], PlatformAccountDirectoryPort]


async def _resolve_or_404(
    broker: HardCapsBrokerPort,
    request: Request,
    platform_account_id: str,
    *,
    directory_factory: _DirectoryFactory,
) -> tuple[AccountHardCapsView, BusinessId]:
    """El broker canonicaliza el id y devuelve la forma canonica; el alcance
    se comprueba con ESA forma, no con la que escribio el cliente
    (`ads-api` no inventa su propia forma del id)."""
    try:
        view = await GetAccountHardCaps(broker).execute(platform_account_id)
    except BrokerRequestDeniedError as exc:
        raise _translate_denial(exc) from exc
    except BrokerConnectionError as exc:
        raise _broker_unavailable() from exc

    container: Container = request.app.state.container
    async with container.session_factory() as db_session:
        directory: PlatformAccountDirectoryPort = directory_factory(db_session)
        business = await directory.business_of(view.platform_account_id)
    if business is None:
        raise _not_found()
    return view, business


def build_hard_caps_router(
    settings: ApiSettings,
    *,
    federated_available: bool = False,
    directory_factory: _DirectoryFactory = SqlPlatformAccountDirectory,
) -> APIRouter:
    broker = BrokerHardCapsSocketClient(settings.broker_socket_path)
    totp_enc_key = settings.totp_enc_key.get_secret_value()
    router = APIRouter(prefix=_ROUTER_PREFIX, tags=["hard-caps"])

    async def _require_presence(
        request: Request,
        owner: AuthenticatedOwner,
        *,
        action_hash: str,
        needs_fresh_identification: bool,
    ) -> str:
        """El 401 de frescura SIEMPRE antes que el 428 de confirmacion: sin
        frescura, la confirmacion ni se invoca.

        Devuelve el nonce de la confirmacion recien gastada. Viaja como
        `request_id` hasta el registro del broker: es lo que enlaza la fila
        de `owner_action_confirmations` con la traza que un compromiso de
        `ads-api` no puede borrar. Ya esta consumido, asi que no autoriza
        nada por si solo."""
        container: Container = request.app.state.container
        async with container.session_factory() as db_session:
            if needs_fresh_identification:
                await require_fresh_identification(
                    request,
                    db_session,
                    owner,
                    action_hash=action_hash,
                    totp_enc_key=totp_enc_key,
                    clock=container.clock,
                    federated_available=federated_available,
                )
            return await require_action_confirmation(
                request, db_session, owner, action_hash=action_hash, clock=container.clock
            )

    @router.get("/{platform_account_id}/hard-caps")
    async def read_hard_caps(
        platform_account_id: str,
        request: Request,
        _owner: AuthenticatedOwner = CURRENT_OWNER,
    ) -> dict[str, Any]:
        view, _business = await _resolve_or_404(
            broker, request, platform_account_id, directory_factory=directory_factory
        )
        return _response(view, currency=_currency_of(view))

    @router.put("/{platform_account_id}/hard-caps")
    async def set_hard_caps(
        platform_account_id: str,
        request: Request,
        body: Annotated[Any, Body(...)],
        owner: AuthenticatedOwner = CURRENT_OWNER,
    ) -> dict[str, Any]:
        caps = _parse_body(body)
        current, business = await _resolve_or_404(
            broker, request, platform_account_id, directory_factory=directory_factory
        )
        request_id = await _require_presence(
            request,
            owner,
            action_hash=_action_hash(current.platform_account_id, caps),
            needs_fresh_identification=relaxes_the_effective_cap(current, caps),
        )
        container: Container = request.app.state.container
        async with container.session_factory() as db_session:
            use_case = SetAccountHardCaps(
                broker,
                RecordDecision(SqlDecisionLogRepository(db_session)),
                business_id=business,
            )
            view = await _apply(
                use_case.execute(
                    current.platform_account_id,
                    caps,
                    owner_id=str(owner.owner_id),
                    request_id=request_id,
                )
            )
            await db_session.commit()
        return _response(view, currency=_currency_of(view, caps.currency))

    @router.delete("/{platform_account_id}/hard-caps")
    async def delete_hard_caps(
        platform_account_id: str,
        request: Request,
        owner: AuthenticatedOwner = CURRENT_OWNER,
    ) -> dict[str, Any]:
        current, business = await _resolve_or_404(
            broker, request, platform_account_id, directory_factory=directory_factory
        )
        request_id = await _require_presence(
            request,
            owner,
            action_hash=_action_hash(current.platform_account_id, None),
            needs_fresh_identification=withdrawal_relaxes_the_effective_cap(current),
        )
        container: Container = request.app.state.container
        async with container.session_factory() as db_session:
            use_case = DeleteAccountHardCaps(
                broker,
                RecordDecision(SqlDecisionLogRepository(db_session)),
                business_id=business,
            )
            view = await _apply(
                use_case.execute(
                    current.platform_account_id,
                    owner_id=str(owner.owner_id),
                    request_id=request_id,
                )
            )
            await db_session.commit()
        return _response(view, currency=_currency_of(view))

    return router


async def _apply(awaitable: Any) -> AccountHardCapsView:  # noqa: ANN401 - corrutina del caso de uso
    """Punto UNICO de traduccion del veredicto del broker a un error HTTP:
    sin el, cada ruta repetiria los dos `except` y una podria olvidarse."""
    try:
        view: AccountHardCapsView = await awaitable
    except BrokerRequestDeniedError as exc:
        raise _translate_denial(exc) from exc
    except BrokerConnectionError as exc:
        raise _broker_unavailable() from exc
    return view
