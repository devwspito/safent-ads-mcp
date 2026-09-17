"""`connect_platform_account`/`get_connection_status` (Anadido del dueno,
15-sep, spec.md P1-9): mismo flujo OAuth que ya usa el panel
(`accounts/presentation/connections_router.py`), envuelto como dos
herramientas MCP de clase `CONNECTION_WRITE` -- solo el permiso `aprobar`
las ve (`registry.py::_CONNECTION_WRITE_NAMES`). `list_platform_accounts`
no cambia: ya es `READ` en `catalog.py` y ya filtra por `business_id`.

Modulo autonomo, mismo aislamiento que `experiment_tools.py`: declara sus
propios handlers sobre `accounts.application.begin_oauth_connect`/
`accounts.infrastructure.sql_connect_repositories`, sin tocar
`handlers.py`/`write_handlers.py`. Nunca devuelve una credencial ni un
secreto de plataforma -- solo la URL de consentimiento del proveedor."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.accounts.application.begin_oauth_connect import BeginOAuthConnect
from safent_ads.accounts.application.connect_ports import OAuthBrokerPort, SoleOwnerLookupPort
from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.application.list_platform_accounts import ListPlatformAccounts
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.accounts.infrastructure.sql_connect_repositories import (
    SqlCredentialRepository,
    SqlOAuthConnectSessionRepository,
)
from safent_ads.accounts.infrastructure.sql_repositories import SqlAccountRepository
from safent_ads.accounts.infrastructure.sql_sole_owner_lookup import SqlSoleOwnerLookup
from safent_ads.mcp.application.caller_scope import OWNER_CALLER_ID, CallerScope, Permission
from safent_ads.mcp.application.errors import ToolValidationError
from safent_ads.mcp.presentation.args import ConnectPlatformAccountArgs, GetConnectionStatusArgs
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition
from safent_ads.shared.ids import BusinessId, IdGenerator
from safent_ads.shared.ids import PlatformCode as AccountsPlatformCode

type Handler[ArgsT, ReturnT] = Callable[[ArgsT, CallerScope], Awaitable[ReturnT]]

_PERSON_CALLER_PREFIX = "person:"
_REDIRECT_TEMPLATE = "{base}/api/v1/platform-accounts/{provider}/reconnect/callback"


@dataclass(frozen=True, slots=True)
class ConnectionToolServices:
    """Piezas para conectar una cuenta de plataforma desde el MCP -- mismo
    `oauth_broker`/`id_generator` que `composition/app.py` cablea para el
    router REST, sin duplicar credenciales de vendor."""

    session_factory: async_sessionmaker[AsyncSession]
    oauth_broker: OAuthBrokerPort
    id_generator: IdGenerator
    public_base_url: str
    # Injectable so tests can pin the owner without a database.
    sole_owner_lookup: Callable[[AsyncSession], SoleOwnerLookupPort] = SqlSoleOwnerLookup


def _caller_person_id(caller_scope: CallerScope) -> uuid.UUID:
    if not caller_scope.caller_id.startswith(_PERSON_CALLER_PREFIX):
        raise ToolValidationError("solo una persona con puesto puede conectar una cuenta")
    try:
        return uuid.UUID(caller_scope.caller_id.removeprefix(_PERSON_CALLER_PREFIX))
    except ValueError as exc:
        raise ToolValidationError("identidad de llamador invalida") from exc


async def _initiator_owner_id(caller_scope: CallerScope, lookup: SoleOwnerLookupPort) -> uuid.UUID:
    """Whom the connection is attributed to: the seat holder or, in single-owner
    mode, the installation's sole owner. Anything else fails closed (2026-09-16:
    the owner's static token was refused here as "sin puesto")."""
    if caller_scope.caller_id.startswith(_PERSON_CALLER_PREFIX):
        return _caller_person_id(caller_scope)
    if caller_scope.caller_id == OWNER_CALLER_ID and caller_scope.permission is Permission.APPROVE:
        owner_id = await lookup.sole_owner_id()
        if owner_id is None:
            raise ToolValidationError("no hay un unico dueño al que atribuir la conexion")
        return owner_id
    raise ToolValidationError("solo una persona con puesto o el dueño pueden conectar una cuenta")


def build_connection_tool_definitions(
    services: ConnectionToolServices,
) -> list[ToolDefinition[Any]]:
    return [
        ToolDefinition(
            name="connect_platform_account",
            description=(
                "Empieza a conectar una nueva cuenta publicitaria de Meta o Google para "
                "este negocio. Devuelve `authorize_url`: un enlace de consentimiento del "
                "proveedor que la persona debe abrir en su navegador (no se puede completar "
                "desde el chat) y `session_id` para consultar el resultado con "
                "`get_connection_status`. Para Google, pasa `google_customer_id` (10 digitos, "
                "con o sin guiones) con la cuenta de Google Ads que quieres conectar; sin el, "
                "la conexion no puede empezar. Solo permiso aprobar: conectar una cuenta es "
                "una decision de empresa, no una lectura ni una propuesta."
            ),
            args_model=ConnectPlatformAccountArgs,
            tool_class=ToolClass.CONNECTION_WRITE,
            handler=_connect_platform_account(services),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="get_connection_status",
            description=(
                "Indica si el negocio ya tiene al menos una cuenta conectada y sana para "
                "`platform` (google|meta): cuentas encontradas, si cada una necesita "
                "reconectar (`needs_reconnect`) y el estado de su credencial. Usala despues "
                "de `connect_platform_account` para confirmar que el consentimiento se "
                "completo antes de proponer campanas en esa cuenta."
            ),
            args_model=GetConnectionStatusArgs,
            tool_class=ToolClass.CONNECTION_WRITE,
            handler=_get_connection_status(services),
            business_id_of=_by_business_id,
        ),
    ]


def _by_business_id(args: Any) -> str:  # noqa: ANN401 - extractor generico por posicion
    return str(args.business_id)


def _connect_platform_account(
    services: ConnectionToolServices,
) -> Handler[ConnectPlatformAccountArgs, dict[str, Any]]:
    async def handle(args: ConnectPlatformAccountArgs, caller_scope: CallerScope) -> dict[str, Any]:
        redirect_uri = _REDIRECT_TEMPLATE.format(
            base=services.public_base_url.rstrip("/"), provider=args.platform.value
        )
        async with services.session_factory() as session:
            owner_id = await _initiator_owner_id(caller_scope, services.sole_owner_lookup(session))
            use_case = BeginOAuthConnect(
                oauth_broker=services.oauth_broker,
                sessions=SqlOAuthConnectSessionRepository(session),
                id_generator=services.id_generator,
            )
            try:
                result = await use_case.execute(
                    provider=AccountsPlatformCode(args.platform.value),
                    business_id=BusinessId.parse(args.business_id),
                    owner_id=owner_id,
                    redirect_uri=redirect_uri,
                    google_customer_id=args.google_customer_id,
                )
            except ValueError as exc:
                raise ToolValidationError(
                    "Introduce un número de cuenta de Google Ads válido: 10 dígitos."
                ) from exc
            except BrokerRequestDeniedError as exc:
                raise ToolValidationError(
                    "La plataforma rechazo empezar la conexion. Revisa las credenciales de "
                    "desarrollador en Conexiones antes de reintentar."
                ) from exc
            except BrokerConnectionError as exc:
                raise ToolValidationError(
                    "El broker de conexiones no responde. Intentalo de nuevo en un momento."
                ) from exc
            await session.commit()
        return {
            "authorize_url": result.authorize_url,
            "session_id": str(result.session_id),
            "expires_at": result.expires_at.isoformat(),
            "message": (
                f"Abre este enlace en tu navegador para autorizar la conexion de "
                f"{args.platform.value}: la persona que complete el consentimiento debe "
                "tener acceso a esa cuenta publicitaria. Vuelve aqui cuando termines."
            ),
        }

    return handle


def _get_connection_status(
    services: ConnectionToolServices,
) -> Handler[GetConnectionStatusArgs, dict[str, Any]]:
    async def handle(args: GetConnectionStatusArgs, _caller_scope: CallerScope) -> dict[str, Any]:
        business_id = BusinessId.parse(args.business_id)
        async with services.session_factory() as session:
            use_case = ListPlatformAccounts(
                SqlAccountRepository(session), SqlCredentialRepository(session)
            )
            views = await use_case.execute(business_id)
        target_platform = AccountsPlatformCode(args.platform.value)
        matches = [view for view in views if view.account_ref.platform == target_platform]
        return {
            "connected": bool(matches),
            "accounts": [
                {
                    "account_ref": str(view.account_ref),
                    "label": view.label,
                    "status": view.status.value,
                    "credential_status": (
                        view.credential_status.value if view.credential_status is not None else None
                    ),
                    "needs_reconnect": view.needs_reconnect,
                }
                for view in matches
            ],
        }

    return handle
