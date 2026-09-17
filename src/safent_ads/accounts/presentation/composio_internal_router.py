"""Owner-session relay of sealed runtime configuration; no secret-bearing DTO."""

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.accounts.infrastructure.oauth_broker_client import OAuthBrokerSocketClient
from safent_ads.composition.settings import ApiSettings
from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError


class SealedComposioLease(BaseModel):
    model_config = ConfigDict(extra="forbid")
    envelope: str = Field(min_length=1, max_length=32768, repr=False)


def build_composio_internal_router(settings: ApiSettings) -> APIRouter:
    router = APIRouter(prefix="/api/v1/internal/composio", include_in_schema=False)
    client = OAuthBrokerSocketClient(settings.broker_socket_path)

    def require_companion(request: Request) -> None:
        if not settings.companion_mode:
            raise ApiError(status_code=404, code="NOT_FOUND", message="No encontrado.")
        if request.headers.get("authorization"):
            raise ApiError(status_code=401, code="UNAUTHORIZED", message="Sesion requerida.")

    @router.get("/channel")
    async def channel(
        request: Request, response: Response, _owner: AuthenticatedOwner = CURRENT_OWNER
    ) -> dict[str, object]:
        require_companion(request)
        response.headers["Cache-Control"] = "no-store"
        try:
            return await client.composio_channel()
        except (BrokerRequestDeniedError, BrokerConnectionError):
            raise ApiError(
                status_code=503,
                code="COMPOSIO_CHANNEL_UNAVAILABLE",
                message="Conexión no disponible.",
            ) from None

    @router.post("/lease")
    async def lease(
        body: SealedComposioLease,
        request: Request,
        response: Response,
        _owner: AuthenticatedOwner = CURRENT_OWNER,
    ) -> dict[str, bool]:
        require_companion(request)
        response.headers["Cache-Control"] = "no-store"
        try:
            await client.accept_composio_lease(body.envelope)
        except (BrokerRequestDeniedError, BrokerConnectionError):
            raise ApiError(
                status_code=409, code="COMPOSIO_LEASE_DENIED", message="Configuración no aceptada."
            ) from None
        return {"accepted": True}

    return router
