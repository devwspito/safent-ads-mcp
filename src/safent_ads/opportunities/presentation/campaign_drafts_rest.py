"""Owner/session+CSRF boundary, separate from executable proposals."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from safent_ads.iam.presentation.errors import ApiError
from safent_ads.opportunities.domain.campaign_draft import DraftError, DraftFields
from safent_ads.opportunities.infrastructure.campaign_drafts_sql import CampaignDraftStore
from safent_ads.panel.presentation.deps import require_business_access

BusinessDep = Annotated[str, Depends(require_business_access)]


class SaveDraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    draft_key: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
    expected_revision: Annotated[int, Field(ge=1)] | None = None
    changes: DraftFields


class PromoteDraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: Annotated[int, Field(ge=1)]


def _error(error: DraftError) -> ApiError:
    status = (
        404
        if error.code.endswith("NOT_FOUND")
        else 409
        if error.code
        in {
            "CAMPAIGN_DRAFT_CHANGED",
            "CAMPAIGN_DRAFT_ALREADY_PROPOSED",
            "CAMPAIGN_DRAFT_PROPOSAL_CONFLICT",
        }
        else 422
    )
    return ApiError(
        status_code=status,
        code=error.code,
        message=(
            "Esta solicitud no ha aprobado ni ejecutado una campaña. "
            "Revisa los campos pendientes o vuelve a cargar la versión actual."
        ),
        details={"missing_fields": list(error.missing)},
    )


def build_campaign_drafts_router(store: CampaignDraftStore) -> APIRouter:
    router = APIRouter(prefix="/api/v1/campaign-drafts", tags=["campaign-drafts"])

    @router.get("")
    async def list_drafts(business_id: BusinessDep) -> dict[str, Any]:
        try:
            return await store.list(business_id)
        except DraftError as exc:
            raise _error(exc) from exc

    @router.get("/{draft_id}")
    async def get_draft(draft_id: UUID, business_id: BusinessDep) -> dict[str, Any]:
        try:
            return await store.get(business_id, str(draft_id))
        except DraftError as exc:
            raise _error(exc) from exc

    @router.post("")
    async def save_draft(business_id: BusinessDep, body: SaveDraftBody) -> dict[str, Any]:
        try:
            return await store.save(
                business_id, body.draft_key, body.expected_revision, body.changes
            )
        except DraftError as exc:
            raise _error(exc) from exc

    @router.post("/{draft_id}/propose")
    async def promote(
        draft_id: UUID, business_id: BusinessDep, body: PromoteDraftBody
    ) -> dict[str, Any]:
        try:
            return await store.promote(business_id, str(draft_id), body.expected_revision)
        except DraftError as exc:
            raise _error(exc) from exc

    return router
