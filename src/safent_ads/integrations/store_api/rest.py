"""Owner-only token input. The response never contains credentials."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.integrations.store_api.service import StoreApiError, StoreApiService
from safent_ads.panel.presentation.deps import require_business_access

BusinessDep = Annotated[str, Depends(require_business_access)]
OwnerDep = Annotated[AuthenticatedOwner, CURRENT_OWNER]


class TokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr = Field(min_length=1, max_length=4096)


def build_store_api_router(service: StoreApiService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/integrations/store-api", tags=["store-api"])

    @router.get("")
    async def status(business_id: BusinessDep) -> dict[str, Any]:
        return await service.status(business_id)

    @router.put("")
    async def connect(business_id: BusinessDep, owner: OwnerDep, body: TokenBody) -> dict[str, Any]:
        try:
            return await service.connect(business_id, body.token.get_secret_value(), owner.owner_id)
        except StoreApiError as exc:
            raise ApiError(
                status_code=422, code="STORE_API_CONNECTION_FAILED", message=str(exc)
            ) from exc

    return router
