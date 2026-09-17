"""Herramientas MCP de `brand` para el rastreo de identidad de marca desde
un sitio web (owner request: "El MCP debe pedir el sitio web del cliente
(OPCIONAL) ... El usuario puede subir manual o poner el enlace").
Manejadores puros en pydantic estricto -- enums cerrados, IDs con patron
(threat-model.md C-11) -- que el `ToolRegistry` del carril de superficie
(fuera de este lane, mismo estado que `creative.presentation.mcp_tools`)
registrara. Este modulo no monta nada: no hay `ToolRegistry` aqui.

F-8 (checklists/website-brand-extractor-review.md, decision del
propietario): `ingest_brand_from_website` NO acepta URL por MCP -- una
inyeccion de prompt convertiria la herramienta en sonda de red arbitraria
cuyo resultado (titulo, h1, copy) vuelve al contexto del modelo. Solo
puede rastrear `BrandKit.confirmed_website_host`, el dominio que un
propietario autenticado ya confirmo por REST
(`application/ingest_brand_from_website.py`); si el negocio todavia no
tiene ninguno, la herramienta lo reporta (`WebsiteDomainNotConfirmedResult`)
y se detiene -- nunca inventa ni acepta un dominio alternativo. La URL
libre solo existe en `POST /api/v1/brand/discover`
(`presentation/router.py`), donde la teclea una persona.
`upload_brand_asset` sigue el mismo principio -- solo referencia un
`asset_id` ya importado via `POST /api/v1/brand/assets`, nunca bytes ni
URLs por MCP."""

from __future__ import annotations

from pydantic import Field

from safent_ads.brand.application.confirm_brand_draft import ConfirmBrandDraft
from safent_ads.brand.application.errors import BrandKitNotFoundError
from safent_ads.brand.application.get_brand_draft import GetBrandDraft
from safent_ads.brand.application.get_brand_kit import GetBrandKit
from safent_ads.brand.application.ingest_brand_from_website import (
    IngestBrandFromWebsite,
    IngestBrandFromWebsiteRequest,
)
from safent_ads.brand.application.upload_brand_asset import (
    SelectExistingBrandAssetRequest,
    UploadBrandAsset,
)
from safent_ads.brand.domain.brand_asset import AssetKind
from safent_ads.brand.domain.discovery import BrandDiscoveryDraft
from safent_ads.brand.presentation.payloads import (
    OPAQUE_ASSET_ID_PATTERN,
    UUID_PATTERN,
    ConfirmBrandDraftPayload,
    StrictModel,
)
from safent_ads.brand.presentation.serializers import brand_kit_detail, draft_detail
from safent_ads.shared.ids import BusinessId

_CONFIRMED_HOST_URL_TEMPLATE = "https://{host}/"


class IngestBrandFromWebsiteArgs(StrictModel):
    business_id: str = Field(pattern=UUID_PATTERN)


class IngestBrandFromWebsiteResult(StrictModel):
    domain_confirmed: bool = True
    source_url: str | None = None
    logo_candidate_count: int = 0
    color_candidate_count: int = 0
    typography_candidate_count: int = 0
    business_name_candidate_count: int = 0
    copy_sample_count: int = 0


class WebsiteDomainNotConfirmedResult(StrictModel):
    """F-8: el negocio no tiene `confirmed_website_host` todavia -- el
    propietario debe rastrear una vez por
    `POST /api/v1/brand/discover` (REST) antes de que esta herramienta
    pueda hacer nada."""

    domain_confirmed: bool = False
    message: str = (
        "Este negocio todavia no tiene un dominio de sitio web confirmado. "
        "El propietario debe rastrearlo primero desde el panel."
    )


class GetBrandDraftArgs(StrictModel):
    business_id: str = Field(pattern=UUID_PATTERN)


class ConfirmBrandDraftArgs(ConfirmBrandDraftPayload):
    business_id: str = Field(pattern=UUID_PATTERN)


class UploadBrandAssetArgs(StrictModel):
    business_id: str = Field(pattern=UUID_PATTERN)
    asset_id: str = Field(pattern=OPAQUE_ASSET_ID_PATTERN)
    kind: AssetKind


class BrandMcpTools:
    """Agrupa las dependencias de los manejadores; el `ToolRegistry` real
    invoca cada metodo por nombre tras autorizar `business_id`
    (`RequireBusinessAccess`, contracts/mcp-tools.md regla 4 -- fuera de
    este carril)."""

    def __init__(
        self,
        *,
        ingest_brand_from_website: IngestBrandFromWebsite,
        get_brand_kit: GetBrandKit,
        get_brand_draft: GetBrandDraft,
        confirm_brand_draft: ConfirmBrandDraft,
        upload_brand_asset: UploadBrandAsset,
    ) -> None:
        self._ingest_brand_from_website = ingest_brand_from_website
        self._get_brand_kit = get_brand_kit
        self._get_brand_draft = get_brand_draft
        self._confirm_brand_draft = confirm_brand_draft
        self._upload_brand_asset = upload_brand_asset

    async def ingest_brand_from_website(
        self, args: IngestBrandFromWebsiteArgs
    ) -> IngestBrandFromWebsiteResult | WebsiteDomainNotConfirmedResult:
        business_id = BusinessId.parse(args.business_id)
        confirmed_host = await self._confirmed_website_host(business_id)
        if confirmed_host is None:
            return WebsiteDomainNotConfirmedResult()
        draft = await self._ingest_brand_from_website.execute(
            IngestBrandFromWebsiteRequest(
                business_id=business_id,
                url=_CONFIRMED_HOST_URL_TEMPLATE.format(host=confirmed_host),
            )
        )
        return _ingest_result(draft)

    async def _confirmed_website_host(self, business_id: BusinessId) -> str | None:
        try:
            brand_kit = await self._get_brand_kit.execute(business_id)
        except BrandKitNotFoundError:
            return None
        return brand_kit.confirmed_website_host

    async def get_brand_draft(self, args: GetBrandDraftArgs) -> dict[str, object]:
        draft = await self._get_brand_draft.execute(BusinessId.parse(args.business_id))
        return draft_detail(draft)

    async def confirm_brand_draft(self, args: ConfirmBrandDraftArgs) -> dict[str, object]:
        kit = await self._confirm_brand_draft.execute(
            args.to_request(BusinessId.parse(args.business_id))
        )
        return brand_kit_detail(kit)

    async def upload_brand_asset(self, args: UploadBrandAssetArgs) -> dict[str, object]:
        candidate = await self._upload_brand_asset.from_existing_candidate(
            SelectExistingBrandAssetRequest(
                business_id=BusinessId.parse(args.business_id),
                asset_id=args.asset_id,
                kind=args.kind,
            )
        )
        return {
            "asset_id": candidate.asset_id,
            "kind": candidate.kind.value,
            "storage_uri": candidate.storage_uri,
            "source": candidate.source.value,
        }


def _ingest_result(draft: BrandDiscoveryDraft) -> IngestBrandFromWebsiteResult:
    return IngestBrandFromWebsiteResult(
        source_url=draft.source_url,
        logo_candidate_count=len(draft.logo_candidates),
        color_candidate_count=len(draft.color_candidates),
        typography_candidate_count=len(draft.typography_candidates),
        business_name_candidate_count=len(draft.business_name_candidates),
        copy_sample_count=len(draft.copy_samples),
    )


__all__ = [
    "BrandMcpTools",
    "ConfirmBrandDraftArgs",
    "GetBrandDraftArgs",
    "IngestBrandFromWebsiteArgs",
    "IngestBrandFromWebsiteResult",
    "UploadBrandAssetArgs",
    "WebsiteDomainNotConfirmedResult",
]
