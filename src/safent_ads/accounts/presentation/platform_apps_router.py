"""Router REST de credenciales de VENDOR (owner decision, app-credentials-ui,
contracts/rest-api.md §Conexiones): `GET /platform-apps`,
`PUT /platform-apps/{platform}`, `DELETE /platform-apps/{platform}`. El
propietario teclea el OAuth de Google y la app de Meta
desde el panel -- nunca por `vendor.env` -- y el bróker los guarda
cifrados (`broker/infrastructure/credential_store.py`). Esta capa NUNCA ve
ni persiste el secreto: valida forma/longitud, reenvía al bróker y relee
el estado ya enmascarado que éste calcula.

Sin `business_id`: las credenciales de VENDOR son por INSTALACIÓN, no por
negocio (misma app de Google/Meta para todos los negocios del
propietario) -- `CURRENT_OWNER` basta, no hace falta
`require_business_access`."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Body, Request
from pydantic import ValidationError
from starlette import status

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.application.platform_apps import (
    DeletePlatformAppCredentials,
    GetPlatformAppStatus,
    SetGooglePlatformAppCredentials,
    SetMetaPlatformAppCredentials,
)
from safent_ads.accounts.application.platform_apps_ports import (
    GoogleAppCredentialsInput,
    MetaAppCredentialsInput,
    PlatformAppStatus,
)
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.accounts.infrastructure.oauth_broker_client import OAuthBrokerSocketClient
from safent_ads.accounts.presentation.platform_apps_payloads import (
    PlatformAppsResponse,
    PlatformAppStatusResponse,
    SetGoogleAppCredentialsRequest,
    SetMetaAppCredentialsRequest,
)
from safent_ads.accounts.presentation.redirect_uri import platform_app_redirect_uri
from safent_ads.audit.application.record_decision import RecordDecision
from safent_ads.audit.domain.entry import ActorKind, DecisionKind, PendingDecision
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.composition.container import Container
from safent_ads.composition.settings import ApiSettings
from safent_ads.iam.presentation.action_confirmation import require_action_confirmation
from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.shared.ids import BusinessId, PlatformCode

logger = structlog.get_logger(__name__)

_DELETE_CONFIRMATION_PHRASE = "ELIMINAR"
# Credenciales de VENDOR: por instalacion, no por negocio -- mismo
# `business_id` nulo que `notifications/domain/pairing.py::UNSCOPED_BUSINESS_ID`
# usa para el emparejamiento de Telegram (tambien por PROPIETARIO), sin
# importar ese modulo de `notifications` solo por una constante y acoplar
# los dos bounded contexts.
_UNSCOPED_BUSINESS_ID = BusinessId.parse("00000000-0000-0000-0000-000000000000")


def _broker_unavailable() -> ApiError:
    return ApiError(
        status_code=status.HTTP_502_BAD_GATEWAY,
        code="BROKER_UNAVAILABLE",
        message="El bróker de conexiones no responde.",
    )


def _broker_denied(exc: BrokerRequestDeniedError) -> ApiError:
    return ApiError(
        status_code=status.HTTP_502_BAD_GATEWAY,
        code="BROKER_DENIED",
        message="El bróker rechazó la operación.",
        details={"error_code": exc.error_code},
    )


def _validation_error(exc: ValidationError) -> ApiError:
    errors = [
        {"field": ".".join(str(part) for part in error["loc"]), "message": error["msg"]}
        for error in exc.errors()
    ]
    return ApiError(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code="VALIDATION_ERROR",
        message="Credenciales inválidas.",
        details={"errors": errors},
    )


def _typed_confirmation_required() -> ApiError:
    return ApiError(
        status_code=428,
        code="TYPED_CONFIRMATION_REQUIRED",
        message=f"Escribe {_DELETE_CONFIRMATION_PHRASE} para confirmar.",
        details={"phrase": _DELETE_CONFIRMATION_PHRASE},
    )


def _require_typed_confirmation(body: dict[str, Any]) -> None:
    typed = str(body.get("typed_confirmation") or "").strip().upper()
    if typed != _DELETE_CONFIRMATION_PHRASE:
        raise _typed_confirmation_required()


def _parse_google_input(body: dict[str, Any]) -> GoogleAppCredentialsInput:
    try:
        parsed = SetGoogleAppCredentialsRequest.model_validate(body)
    except ValidationError as exc:
        raise _validation_error(exc) from exc
    return GoogleAppCredentialsInput(
        client_id=parsed.client_id,
        client_type=parsed.client_type,
        client_secret=parsed.client_secret,
        login_customer_id=parsed.login_customer_id,
    )


def _parse_meta_input(body: dict[str, Any]) -> MetaAppCredentialsInput:
    try:
        parsed = SetMetaAppCredentialsRequest.model_validate(body)
    except ValidationError as exc:
        raise _validation_error(exc) from exc
    return MetaAppCredentialsInput(app_id=parsed.app_id, app_secret=parsed.app_secret)


def _set_action_hash(owner_id: uuid.UUID, platform: PlatformCode) -> str:
    return f"platform_app_credentials_set|{owner_id}|{platform.value}"


def _to_response(
    settings: ApiSettings, status_result: PlatformAppStatus, platform: PlatformCode,
    request: Request,
) -> PlatformAppStatusResponse:
    return PlatformAppStatusResponse.from_status(
        status_result, redirect_uri=platform_app_redirect_uri(settings, platform, request)
    )


async def _fetch_status(
    oauth_broker: OAuthBrokerSocketClient, settings: ApiSettings, platform: PlatformCode,
    request: Request,
) -> PlatformAppStatusResponse:
    try:
        status_result = await GetPlatformAppStatus(oauth_broker).execute(platform)
    except BrokerConnectionError as exc:
        raise _broker_unavailable() from exc
    return _to_response(settings, status_result, platform, request)


def build_platform_apps_router(settings: ApiSettings) -> APIRouter:
    oauth_broker = OAuthBrokerSocketClient(settings.broker_socket_path)
    router = APIRouter(prefix="/api/v1/platform-apps", tags=["platform-apps"])

    @router.get("", response_model=PlatformAppsResponse)
    async def list_platform_apps(
        request: Request,
        _owner: AuthenticatedOwner = CURRENT_OWNER,
    ) -> PlatformAppsResponse:
        items = [
            await _fetch_status(oauth_broker, settings, platform, request)
            for platform in PlatformCode
        ]
        return PlatformAppsResponse(items=items)

    @router.put("/{platform}", response_model=PlatformAppStatusResponse)
    async def set_platform_app_credentials(
        platform: PlatformCode,
        request: Request,
        body: Annotated[dict[str, Any], Body(...)],
        owner: AuthenticatedOwner = CURRENT_OWNER,
    ) -> PlatformAppStatusResponse:
        container: Container = request.app.state.container
        async with container.session_factory() as db_session:
            await require_action_confirmation(
                request,
                db_session,
                owner,
                action_hash=_set_action_hash(owner.owner_id, platform),
                clock=container.clock,
            )
            try:
                if platform == PlatformCode.GOOGLE:
                    status_result = await SetGooglePlatformAppCredentials(oauth_broker).execute(
                        _parse_google_input(body)
                    )
                else:
                    status_result = await SetMetaPlatformAppCredentials(oauth_broker).execute(
                        _parse_meta_input(body)
                    )
            except BrokerRequestDeniedError as exc:
                logger.warning(
                    "platform_app_credentials_set_denied",
                    platform=platform.value,
                    error_code=exc.error_code,
                )
                raise _broker_denied(exc) from exc
            except BrokerConnectionError as exc:
                raise _broker_unavailable() from exc

            await RecordDecision(SqlDecisionLogRepository(db_session)).execute(
                PendingDecision(
                    business_id=_UNSCOPED_BUSINESS_ID,
                    kind=DecisionKind.PLATFORM_APP_CREDENTIALS_SET,
                    actor_kind=ActorKind.OWNER,
                    actor_id=owner.email,
                    payload={"platform": platform.value},
                )
            )
            await db_session.commit()
        return _to_response(settings, status_result, platform, request)

    @router.delete("/{platform}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_platform_app_credentials(
        platform: PlatformCode,
        request: Request,
        body: Annotated[dict[str, Any], Body(...)],
        owner: AuthenticatedOwner = CURRENT_OWNER,
    ) -> None:
        _require_typed_confirmation(body)
        container: Container = request.app.state.container
        try:
            await DeletePlatformAppCredentials(oauth_broker).execute(platform)
        except BrokerConnectionError as exc:
            raise _broker_unavailable() from exc

        async with container.session_factory() as db_session:
            await RecordDecision(SqlDecisionLogRepository(db_session)).execute(
                PendingDecision(
                    business_id=_UNSCOPED_BUSINESS_ID,
                    kind=DecisionKind.PLATFORM_APP_CREDENTIALS_DELETED,
                    actor_kind=ActorKind.OWNER,
                    actor_id=owner.email,
                    payload={"platform": platform.value},
                )
            )
            await db_session.commit()

    return router
