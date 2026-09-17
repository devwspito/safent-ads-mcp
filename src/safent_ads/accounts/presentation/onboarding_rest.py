"""Router REST de `GET /api/v1/onboarding` (029 T022, contracts/rest-api.md
§Onboarding): estado agregado de las credenciales de VENDOR y las cuentas
conectadas que el puente same-origin `/ads/` (026) lee para que la barra
lateral de Safent distinga `unauthorized` de `no_accounts` en vez de un
`unreachable` generico. Por instalacion, sin `business_id` -- mismo
criterio que `platform_apps_router.py` (`CURRENT_OWNER` basta)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette import status as http_status

from safent_ads.accounts.application.create_initial_business import (
    BusinessAlreadyConfiguredError,
    CreateInitialBusiness,
    OwnerConfigurationAmbiguousError,
)
from safent_ads.accounts.application.get_onboarding_status import GetOnboardingStatus
from safent_ads.accounts.application.platform_apps import GetPlatformAppStatus
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.accounts.infrastructure.oauth_broker_client import OAuthBrokerSocketClient
from safent_ads.accounts.infrastructure.sql_business_bootstrap import SqlInitialBusinessRepository
from safent_ads.accounts.infrastructure.sql_repositories import SqlAccountRepository
from safent_ads.accounts.presentation.onboarding_payloads import (
    InitialBusinessRequest,
    OnboardingStatusResponse,
)
from safent_ads.composition.container import Container
from safent_ads.composition.settings import ApiSettings
from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.iam.presentation.schemas import BusinessSummary


def _broker_unavailable() -> ApiError:
    # Mismo codigo que `platform_apps_router.py`: el puente `/ads/` puede
    # tratar este 502 como `unreachable` sin re-adivinar el motivo exacto.
    return ApiError(
        status_code=http_status.HTTP_502_BAD_GATEWAY,
        code="BROKER_UNAVAILABLE",
        message="El bróker de conexiones no responde.",
    )


def build_onboarding_router(settings: ApiSettings) -> APIRouter:
    oauth_broker = OAuthBrokerSocketClient(settings.broker_socket_path)
    router = APIRouter(prefix="/api/v1/onboarding", tags=["onboarding"])

    @router.post(
        "/business", status_code=http_status.HTTP_201_CREATED, response_model=BusinessSummary
    )
    async def create_initial_business(
        payload: InitialBusinessRequest,
        request: Request,
        owner: AuthenticatedOwner = CURRENT_OWNER,
    ) -> BusinessSummary:
        # create_managed_app does not mount this owner surface. A second guard
        # keeps standalone registration of this router closed in that profile.
        if settings.managed_central:
            raise ApiError(status_code=404, code="NOT_FOUND", message="No encontrado.")
        container: Container = request.app.state.container
        async with container.session_factory() as db_session:
            try:
                business = await CreateInitialBusiness(
                    SqlInitialBusinessRepository(db_session)
                ).execute(
                    owner_id=owner.owner_id,
                    name=payload.name,
                    timezone=payload.timezone,
                    reference_currency=payload.reference_currency,
                )
            except OwnerConfigurationAmbiguousError as exc:
                raise ApiError(
                    status_code=403,
                    code="OWNER_CONFIGURATION_AMBIGUOUS",
                    message="La configuración local no tiene un único propietario verificable.",
                ) from exc
            except BusinessAlreadyConfiguredError as exc:
                raise ApiError(
                    status_code=409,
                    code="BUSINESS_ALREADY_CONFIGURED",
                    message="El negocio inicial ya existe. Actualiza tu sesión para seleccionarlo.",
                ) from exc
            await db_session.commit()
        return BusinessSummary(
            business_id=business.business_id.value, slug=business.slug, name=business.name
        )

    @router.get("", response_model=OnboardingStatusResponse)
    async def get_onboarding_status(
        request: Request,
        _owner: AuthenticatedOwner = CURRENT_OWNER,
    ) -> OnboardingStatusResponse:
        container: Container = request.app.state.container
        async with container.session_factory() as db_session:
            use_case = GetOnboardingStatus(
                GetPlatformAppStatus(oauth_broker), SqlAccountRepository(db_session)
            )
            try:
                status = await use_case.execute()
            except BrokerConnectionError as exc:
                raise _broker_unavailable() from exc
        return OnboardingStatusResponse.from_status(status)

    return router
