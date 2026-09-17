"""`IngestBrandFromWebsite`: caso de uso que respalda la herramienta MCP
`ingest_brand_from_website` y `POST /api/v1/brand/discover` (owner
request: "el usuario ... pone el enlace y que se rastree desde la web").

Guarda el borrador Y ademas actualiza el `BrandKit` del negocio con una
vista previa `is_confirmed=False` (`BrandDiscoveryDraft.merge_into_kit`)
para que `get_project_context`/`get_capabilities` (fuera de este lane)
reflejen de inmediato que hay una marca detectada pero pendiente de
confirmar -- nunca se usa como fuente de verdad hasta
`ConfirmBrandDraft`.

F-8 (checklists/website-brand-extractor-review.md): cada `execute()`
exitoso fija `BrandKit.confirmed_website_host` al host de `request.url`.
Para la ruta REST (`POST /api/v1/brand/discover`, propietario autenticado
tecleando la URL) esto ES la confirmacion. La herramienta MCP nunca pasa
un `url` propio (`presentation/mcp_tools.py`): construye la suya a partir
de ESTE mismo campo ya guardado, asi que su propia llamada solo puede
reconfirmar el dominio que ya tenia -- nunca introducir uno nuevo."""

from __future__ import annotations

from dataclasses import dataclass, replace

from safent_ads.brand.application.errors import BrandWebsiteUnreachableError
from safent_ads.brand.application.ports import (
    BrandDiscoveryDraftRepository,
    BrandKitRepository,
    WebsiteBrandDiscoveryPort,
)
from safent_ads.brand.domain.discovery import BrandDiscoveryDraft, validate_discovery_url
from safent_ads.brand.domain.identifiers import BrandKitId
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import InfrastructureError
from safent_ads.shared.ids import BusinessId


@dataclass(frozen=True, kw_only=True)
class IngestBrandFromWebsiteRequest:
    business_id: BusinessId
    url: str


class IngestBrandFromWebsite:
    def __init__(
        self,
        *,
        discovery: WebsiteBrandDiscoveryPort,
        drafts: BrandDiscoveryDraftRepository,
        brand_kits: BrandKitRepository,
        clock: Clock,
    ) -> None:
        self._discovery = discovery
        self._drafts = drafts
        self._brand_kits = brand_kits
        self._clock = clock

    async def execute(self, request: IngestBrandFromWebsiteRequest) -> BrandDiscoveryDraft:
        confirmed_host = validate_discovery_url(request.url)
        draft = await self._discover_or_raise(request.business_id, request.url)
        await self._drafts.save(draft)
        await self._save_unconfirmed_preview(draft, confirmed_website_host=confirmed_host)
        return draft

    async def _discover_or_raise(self, business_id: BusinessId, url: str) -> BrandDiscoveryDraft:
        try:
            return await self._discovery.discover(business_id, url)
        except InfrastructureError as exc:
            raise BrandWebsiteUnreachableError(
                f"no se pudo rastrear el sitio indicado para el negocio {business_id}"
            ) from exc

    async def _save_unconfirmed_preview(
        self, draft: BrandDiscoveryDraft, *, confirmed_website_host: str
    ) -> None:
        existing = await self._brand_kits.get_by_business(draft.business_id)
        brand_kit_id = existing.brand_kit_id if existing else BrandKitId.new()
        preview = draft.merge_into_kit(
            brand_kit_id=brand_kit_id, existing=existing, now=self._clock.now()
        )
        preview = replace(preview, confirmed_website_host=confirmed_website_host)
        await self._brand_kits.save(preview)
