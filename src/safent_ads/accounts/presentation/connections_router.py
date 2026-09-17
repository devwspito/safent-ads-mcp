"""Router REST de conexiones OAuth (contracts/rest-api.md §Conexiones):
`POST .../{id}/reconnect/start`, `GET .../{id}/reconnect/status`,
`GET .../{id}/reconnect/callback`, mas dos rutas nuevas aditivas al
contrato: `POST .../{id}/revoke` y `POST .../meta/system-user-token`.

`start`/`status`/`callback` sirven tanto la conexion inicial como la
reconexion de un proveedor con el MISMO caso de uso de `application`
(`BeginOAuthConnect`/`CompleteOAuthConnect` no conocen ningun
`platform_account_id`, solo `(business_id, provider)`): Google/Meta
autorizan al propietario, no a una cuenta concreta, y
`listAccessibleCustomers`/`me/adaccounts` puede devolver varias cuentas de
una sola vez. Por eso el `{id}` de esas tres rutas es el codigo de
plataforma (`google`|`meta`), no un `AccountRef` -- el `redirect_uri` que
se registra una vez en la consola de cada proveedor tiene que ser fijo, y
un `AccountRef` por cuenta no lo seria (`_redirect_uri`).

`revoke` es la excepcion: revoca la credencial de una cuenta YA
descubierta, asi que su `{id}` es el `AccountRef` (`google:123`,
`AccountRef.parse`, domain/refs.py).

Autorizacion real de `iam/presentation` en las cinco rutas
(threat-model.md C-27): `CURRENT_OWNER`, mas `require_business_access`
(mismo `Depends` que `iam/presentation/router.py`) en las tres que llevan
`business_id` como query param explicito. `revoke` deriva el negocio de la
propia cuenta (su `{id}` ya lo fija) en vez de aceptar uno aparte que
podria no coincidir. `callback` no lleva sesion a proposito
(contracts/rest-api.md: "el callback no la necesita, autoriza el
`state`")."""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Any, Final

import structlog
from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status
from starlette.background import BackgroundTask

from safent_ads.accounts.application.begin_oauth_connect import (
    BeginOAuthConnect,
    BeginOAuthConnectResult,
)
from safent_ads.accounts.application.complete_oauth_connect import CompleteOAuthConnect
from safent_ads.accounts.application.errors import (
    AccountNotFoundError,
    BrokerRequestDeniedError,
    CredentialNotFoundError,
    OAuthProviderDeniedError,
    OAuthSessionExpiredError,
    OAuthSessionNotFoundError,
)
from safent_ads.accounts.application.get_oauth_connect_status import GetOAuthConnectStatus
from safent_ads.accounts.application.list_platform_accounts import ListPlatformAccounts
from safent_ads.accounts.application.oauth_state_hash import hash_state
from safent_ads.accounts.application.register_meta_system_user_token import (
    RegisterMetaSystemUserToken,
)
from safent_ads.accounts.application.revoke_platform_credential import RevokePlatformCredential
from safent_ads.accounts.domain.errors import InvalidStateTransitionError
from safent_ads.accounts.domain.google_customer_id import normalize_google_customer_id
from safent_ads.accounts.domain.oauth_connect_session import OAuthConnectSession, OAuthSessionStatus
from safent_ads.accounts.domain.platform_account import PlatformAccount
from safent_ads.accounts.domain.refs import AccountRef, AccountRefFormatError
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.accounts.infrastructure.oauth_broker_client import OAuthBrokerSocketClient
from safent_ads.accounts.infrastructure.sql_connect_repositories import (
    SqlCredentialRepository,
    SqlOAuthConnectSessionRepository,
)
from safent_ads.accounts.infrastructure.sql_repositories import SqlAccountRepository
from safent_ads.accounts.presentation.payloads import (
    BeginConnectRequest,
    BeginConnectResponse,
    ConnectedAccountsResponse,
    ConnectedAccountSummary,
    ConnectStatusResponse,
    MetaSystemUserTokenRequest,
)
from safent_ads.accounts.presentation.platform_accounts_rest import platform_account_to_json
from safent_ads.accounts.presentation.redirect_uri import platform_app_redirect_uri
from safent_ads.composition.container import Container
from safent_ads.composition.settings import ApiSettings
from safent_ads.iam.presentation.dependencies import (
    CURRENT_OWNER,
    AuthenticatedOwner,
    require_business_access,
)
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId, IdGenerator, PlatformCode

logger = structlog.get_logger(__name__)
_OAUTH_MAX_QUERY_BYTES = 8192
_OAUTH_MAX_STATE_BYTES = 128
_OAUTH_MAX_CODE_BYTES = 4096

_CLOSE_WINDOW_HTML: Final = (
    "<!doctype html><title>Safent Ads</title><p>Vuelve a Safent para comprobar "
    "el resultado de la conexión. Puedes cerrar esta ventana.</p>"
)

_STATUS_ERROR_MESSAGES: Final[dict[str, str]] = {
    "GOOGLE_ACCOUNT_SELECTION_REQUIRED": (
        "Introduce el número de tu cuenta de Google Ads y vuelve a conectar. "
        "Lo encontrarás arriba en Google Ads; no es una contraseña."
    ),
    "GOOGLE_ACCOUNT_NOT_ENABLED": (
        "Google indica que esta cuenta publicitaria está cerrada o no está habilitada. "
        "Revisa su estado en Google Ads o conecta otra cuenta."
    ),
    "GOOGLE_ACCOUNT_ACCESS_DENIED": (
        "Google no permite acceder a la cuenta elegida. Comprueba el número y "
        "conecta con un usuario que tenga acceso a ella."
    ),
    "OAUTH_SESSION_EXPIRED": "La conexión ha caducado, vuelve a intentarlo.",
    "OAUTH_PROVIDER_DENIED": "La plataforma rechazó la conexión.",
    "OAUTH_NO_ACCESSIBLE_ACCOUNTS": (
        "No se encontró ninguna cuenta publicitaria accesible. Vuelve a conectar y "
        "autoriza al menos una cuenta publicitaria a la que tengas acceso."
    ),
    "BROKER_UNAVAILABLE": "No se pudo terminar de conectar. Vuelve a intentarlo.",
    "OAUTH_COMPLETION_FAILED": "No se pudo guardar la conexión. Vuelve a intentarlo.",
    "GOOGLE_PROJECT_ACCESS_LEVEL_TEST": (
        "El proyecto de Google Cloud de tu cliente OAuth solo tiene acceso de prueba. "
        "Solicita acceso a cuentas reales (Explorer, Básico o Estándar) en "
        "https://console.cloud.google.com/google/ads-apis/overview y vuelve a conectar. "
        "No necesitas un token de desarrollador."
    ),
    "OAUTH_SESSION_NOT_FOUND": "La conexión no es válida o ya se completó.",
}
_DEFAULT_STATUS_ERROR_MESSAGE: Final = "No se pudo completar la conexión."

# `CompleteOAuthConnect.execute` puede fallar por una carrera real (dos
# callbacks concurrentes con el mismo `state`, C-26): `OAuthConnectSession`
# rechaza el segundo `mark_ok`/`mark_error` a nivel de dominio. El callback
# siempre responde el mismo HTML minimo pase lo que pase (contrato: "nunca
# filtra" si el `state` existio o no).
_CALLBACK_RESOLUTION_FAILURES: Final = (
    OAuthSessionNotFoundError,
    OAuthSessionExpiredError,
    OAuthProviderDeniedError,
)

_BusinessIdDep = Annotated[uuid.UUID, Depends(require_business_access)]
_SessionIdQuery = Annotated[uuid.UUID, Query()]


def _broker_unavailable() -> ApiError:
    return ApiError(
        status_code=status.HTTP_502_BAD_GATEWAY,
        code="BROKER_UNAVAILABLE",
        message="El bróker de conexiones no responde.",
    )


def _provider_denied() -> ApiError:
    return ApiError(
        status_code=status.HTTP_502_BAD_GATEWAY,
        code="OAUTH_PROVIDER_DENIED",
        message="La plataforma rechazó la conexión.",
    )


def _not_found() -> ApiError:
    return ApiError(
        status_code=status.HTTP_404_NOT_FOUND, code="NOT_FOUND", message="No encontrado."
    )


def _invalid_account_id() -> ApiError:
    return ApiError(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code="VALIDATION_ERROR",
        message="Identificador de cuenta inválido.",
    )


def _already_revoked() -> ApiError:
    return ApiError(
        status_code=status.HTTP_409_CONFLICT,
        code="CREDENTIAL_ALREADY_REVOKED",
        message="La credencial ya estaba revocada.",
    )


def _platform_app_not_configured() -> ApiError:
    return ApiError(
        status_code=status.HTTP_409_CONFLICT,
        code="PLATFORM_APP_NOT_CONFIGURED",
        message=(
            "Faltan las credenciales de desarrollador de esta plataforma. "
            "Configúralas en Conexiones antes de conectar una cuenta."
        ),
    )


def _status_message(error_code: str | None) -> str | None:
    if error_code is None:
        return None
    return _STATUS_ERROR_MESSAGES.get(error_code, _DEFAULT_STATUS_ERROR_MESSAGE)


async def _begin_connect(
    *,
    db_session: AsyncSession,
    oauth_broker: OAuthBrokerSocketClient,
    id_generator: IdGenerator,
    provider: PlatformCode,
    business_id: BusinessId,
    owner_id: uuid.UUID,
    redirect_uri: str,
    google_customer_id: str | None = None,
) -> BeginOAuthConnectResult:
    use_case = BeginOAuthConnect(
        oauth_broker=oauth_broker,
        sessions=SqlOAuthConnectSessionRepository(db_session),
        id_generator=id_generator,
    )
    try:
        return await use_case.execute(
            provider=provider,
            business_id=business_id,
            owner_id=owner_id,
            redirect_uri=redirect_uri,
            google_customer_id=google_customer_id,
        )
    except BrokerRequestDeniedError as exc:
        if exc.error_code == "GOOGLE_ACCOUNT_SELECTION_REQUIRED":
            raise ApiError(
                status_code=422, code=exc.error_code,
                message=_STATUS_ERROR_MESSAGES[exc.error_code],
            ) from exc
        if exc.error_code == "PLATFORM_APP_NOT_CONFIGURED":
            raise _platform_app_not_configured() from exc
        logger.warning("oauth_begin_denied", provider=provider.value, error_code=exc.error_code)
        raise _provider_denied() from exc
    except BrokerConnectionError as exc:
        raise _broker_unavailable() from exc


async def _get_connect_status(
    *, db_session: AsyncSession, business_id: BusinessId, session_id: uuid.UUID, clock: Clock
) -> OAuthConnectSession:
    use_case = GetOAuthConnectStatus(SqlOAuthConnectSessionRepository(db_session), clock)
    try:
        return await use_case.execute(business_id=business_id, session_id=session_id)
    except OAuthSessionNotFoundError as exc:
        raise _not_found() from exc


async def _complete_connect(
    *,
    db_session: AsyncSession,
    oauth_broker: OAuthBrokerSocketClient,
    clock: Clock,
    provider: PlatformCode,
    state: str,
    code: str | None,
) -> None:
    use_case = CompleteOAuthConnect(
        oauth_broker=oauth_broker,
        sessions=SqlOAuthConnectSessionRepository(db_session),
        accounts=SqlAccountRepository(db_session),
        credentials=SqlCredentialRepository(db_session),
        clock=clock,
    )
    try:
        await use_case.execute(state=state, code=code, provider=provider)
    except _CALLBACK_RESOLUTION_FAILURES:
        logger.info("oauth_callback_could_not_resolve_session", provider=provider.value)


async def _complete_connect_background(
    *, container: Container, oauth_broker: OAuthBrokerSocketClient,
    provider: PlatformCode, state: str, code: str | None,
) -> None:
    """Run after the response body, on the DB engine's event loop.

    Each completion owns its session/transaction; no request session or engine
    crosses threads. The existing SQL row lock serializes callbacks/replays.
    Process interruption leaves WAITING, which status polling expires durably.
    """
    try:
        async with container.session_factory() as db_session:
            await _complete_connect(
                db_session=db_session, oauth_broker=oauth_broker, clock=container.clock,
                provider=provider, state=state, code=code,
            )
            await db_session.commit()
    except Exception as exc:
        # Roll back partial inventory before recording a terminal outcome in a
        # fresh transaction. Never log callback fields or exception messages.
        error_code = (
            "BROKER_UNAVAILABLE" if isinstance(exc, BrokerConnectionError)
            else "OAUTH_COMPLETION_FAILED"
        )
        logger.warning("oauth_callback_failed", provider=provider.value, error_code=error_code)
        try:
            async with container.session_factory() as db_session:
                sessions = SqlOAuthConnectSessionRepository(db_session)
                session = await sessions.get_by_state_hash(hash_state(state))
                if (
                    session is not None and session.provider == provider
                    and session.status == OAuthSessionStatus.WAITING
                ):
                    now = container.clock.now()
                    if not session.expire(at=now):
                        session.mark_error(error_code=error_code, at=now)
                    await sessions.save(session)
                    await db_session.commit()
        except Exception:
            # An unavailable DB is recoverable on the next status read via TTL.
            logger.warning("oauth_callback_result_unavailable", provider=provider.value)


async def _revoke_credential(
    *,
    db_session: AsyncSession,
    oauth_broker: OAuthBrokerSocketClient,
    clock: Clock,
    account_ref: AccountRef,
) -> None:
    accounts = SqlAccountRepository(db_session)
    account = await accounts.get_by_ref(account_ref)
    if account is None:
        raise _not_found()

    use_case = RevokePlatformCredential(
        accounts=accounts,
        credentials=SqlCredentialRepository(db_session),
        oauth_broker=oauth_broker,
        clock=clock,
    )
    try:
        await use_case.execute(business_id=account.business_id, account_ref=account_ref)
    except (AccountNotFoundError, CredentialNotFoundError) as exc:
        raise _not_found() from exc
    except InvalidStateTransitionError as exc:
        raise _already_revoked() from exc
    except BrokerRequestDeniedError as exc:
        raise _provider_denied() from exc
    except BrokerConnectionError as exc:
        raise _broker_unavailable() from exc


async def _list_platform_accounts(
    *, db_session: AsyncSession, business_id: BusinessId
) -> list[Any]:
    use_case = ListPlatformAccounts(
        SqlAccountRepository(db_session), SqlCredentialRepository(db_session)
    )
    return list(await use_case.execute(business_id))


async def _register_meta_system_user_token(
    *,
    db_session: AsyncSession,
    oauth_broker: OAuthBrokerSocketClient,
    business_id: BusinessId,
    token: str,
    owner_id: str | None = None,
) -> list[PlatformAccount]:
    use_case = RegisterMetaSystemUserToken(
        oauth_broker=oauth_broker,
        accounts=SqlAccountRepository(db_session),
        credentials=SqlCredentialRepository(db_session),
    )
    try:
        return list(await use_case.execute(business_id=business_id, token=token, owner_id=owner_id))
    except OAuthProviderDeniedError as exc:
        logger.info("meta_system_user_token_rejected", business_id=str(business_id))
        raise _provider_denied() from exc
    except BrokerConnectionError as exc:
        raise _broker_unavailable() from exc


def build_connections_router(settings: ApiSettings) -> APIRouter:
    oauth_broker = OAuthBrokerSocketClient(settings.broker_socket_path)
    router = APIRouter(prefix="/api/v1/platform-accounts", tags=["connections"])

    @router.get("")
    async def list_accounts(
        request: Request,
        business_id: _BusinessIdDep,
        _owner: AuthenticatedOwner = CURRENT_OWNER,
    ) -> dict[str, Any]:
        container: Container = request.app.state.container
        async with container.session_factory() as db_session:
            views = await _list_platform_accounts(
                db_session=db_session, business_id=BusinessId(business_id)
            )
        now = container.clock.now()
        return {"items": [platform_account_to_json(view, now=now) for view in views]}

    @router.post(
        "/{provider}/reconnect/start",
        status_code=status.HTTP_201_CREATED,
        response_model=BeginConnectResponse,
    )
    async def start(
        provider: PlatformCode,
        request: Request,
        business_id: _BusinessIdDep,
        payload: Annotated[BeginConnectRequest | None, Body()] = None,
        owner: AuthenticatedOwner = CURRENT_OWNER,
    ) -> BeginConnectResponse:
        try:
            customer_id = normalize_google_customer_id(
                payload.google_customer_id if payload else None, provider=provider,
            )
        except ValueError as exc:
            raise ApiError(
                status_code=422, code="VALIDATION_ERROR",
                message="Introduce un número de cuenta de Google Ads válido: 10 dígitos.",
            ) from exc
        container: Container = request.app.state.container
        async with container.session_factory() as db_session:
            result = await _begin_connect(
                db_session=db_session,
                oauth_broker=oauth_broker,
                id_generator=container.id_generator,
                provider=provider,
                business_id=BusinessId(business_id),
                owner_id=owner.owner_id,
                redirect_uri=platform_app_redirect_uri(settings, provider, request),
                google_customer_id=customer_id,
            )
            await db_session.commit()
        return BeginConnectResponse.from_result(result)

    @router.get("/{provider}/reconnect/status", response_model=ConnectStatusResponse)
    async def get_status(
        provider: PlatformCode,
        request: Request,
        business_id: _BusinessIdDep,
        session_id: _SessionIdQuery,
        _owner: AuthenticatedOwner = CURRENT_OWNER,
    ) -> ConnectStatusResponse:
        logger.debug("oauth_status_checked", provider=provider.value)
        container: Container = request.app.state.container
        async with container.session_factory() as db_session:
            session = await _get_connect_status(
                db_session=db_session, business_id=BusinessId(business_id), session_id=session_id,
                clock=container.clock,
            )
            await db_session.commit()
        return ConnectStatusResponse.from_session(
            session, message=_status_message(session.error_code)
        )

    @router.get("/{provider}/reconnect/callback", response_class=HTMLResponse)
    async def callback(
        provider: PlatformCode,
        request: Request,
    ) -> HTMLResponse:
        query = request.query_params
        state = query.get("state", "")
        code = query.get("code")
        managed_invalid = False
        if "managed" in query:
            connected_id = query.get("connected_account_id")
            managed_invalid = (
                query.get("managed") != "1"
                or query.get("status") not in {"success", "failed"}
                or any(key in query for key in ("code", "error"))
                or any(
                    len(query.getlist(k)) > 1
                    for k in (
                        "managed",
                        "status",
                        "connected_account_id",
                    )
                )
                or (
                    connected_id is not None
                    and re.fullmatch(
                        r"[A-Za-z0-9_-]{1,200}",
                        connected_id,
                    )
                    is None
                )
                or (query.get("status") == "success" and not connected_id)
            )
            code = connected_id if query.get("status") == "success" else None
        # Callback is independently authenticated by the one-use state, not
        # a browser session. Reject ambiguous inputs before touching the broker.
        if (
            not state
            or managed_invalid
            or ("managed" not in query and bool(code) == bool(query.get("error")))
            or len(request.scope.get("query_string", b"")) > _OAUTH_MAX_QUERY_BYTES
            or len(state) > _OAUTH_MAX_STATE_BYTES
            or (code is not None and len(code) > _OAUTH_MAX_CODE_BYTES)
            or any(len(query.getlist(k)) > 1 for k in ("state", "code", "error"))
        ):
            request.scope["query_string"] = b""
            return HTMLResponse(
                _CLOSE_WINDOW_HTML,
                status_code=400,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
        request.scope["query_string"] = b""

        container: Container = request.app.state.container
        return HTMLResponse(
            _CLOSE_WINDOW_HTML,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            background=BackgroundTask(
                _complete_connect_background, container=container, oauth_broker=oauth_broker,
                provider=provider, state=state, code=code,
            ),
        )

    @router.post("/{account_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
    async def revoke(
        account_id: str,
        request: Request,
        _owner: AuthenticatedOwner = CURRENT_OWNER,
    ) -> None:
        try:
            account_ref = AccountRef.parse(account_id)
        except AccountRefFormatError as exc:
            raise _invalid_account_id() from exc

        container: Container = request.app.state.container
        async with container.session_factory() as db_session:
            await _revoke_credential(
                db_session=db_session,
                oauth_broker=oauth_broker,
                clock=container.clock,
                account_ref=account_ref,
            )
            await db_session.commit()

    @router.post(
        "/meta/system-user-token",
        status_code=status.HTTP_201_CREATED,
        response_model=ConnectedAccountsResponse,
    )
    async def register_meta_system_user_token(
        payload: MetaSystemUserTokenRequest,
        request: Request,
        business_id: _BusinessIdDep,
        _owner: AuthenticatedOwner = CURRENT_OWNER,
    ) -> ConnectedAccountsResponse:
        container: Container = request.app.state.container
        async with container.session_factory() as db_session:
            accounts_connected = await _register_meta_system_user_token(
                db_session=db_session,
                oauth_broker=oauth_broker,
                business_id=BusinessId(business_id),
                token=payload.token,
                owner_id=str(_owner.owner_id),
            )
            await db_session.commit()
        return ConnectedAccountsResponse(
            accounts=[ConnectedAccountSummary.from_platform_account(a) for a in accounts_connected]
        )

    return router
