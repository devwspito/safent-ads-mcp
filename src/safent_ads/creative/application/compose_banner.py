"""`ComposeBanner` (plan.md §5 `creative`): compone el copy exacto sobre un
fondo con `BannerComposerPort` (HTML/Playwright). El copy nunca sale del
render generativo (ver `generate_creative_assets.py` y
`infra/creative/workflows/README.md`)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from safent_ads.creative.application.ports import BannerComposerPort, CreativeAssetRepository
from safent_ads.creative.domain.brand_kit import BrandKit
from safent_ads.creative.domain.copy import AdCopy
from safent_ads.creative.domain.creative_asset import CreativeAsset, Provenance
from safent_ads.creative.domain.enums import Format, GenerationStatus, RendererName
from safent_ads.creative.domain.identifiers import AssetId, AssetRef, BriefId, SignalId
from safent_ads.creative.domain.render_specs import BannerSpec, RenderedAsset
from safent_ads.creative.domain.renderer_selector import MODEL_NAME_BY_RENDERER
from safent_ads.shared.ids import BusinessId

_BANNER_COMPOSER_NAME = RendererName.HTML_BANNER_COMPOSER


@dataclass(frozen=True, kw_only=True)
class ComposeBannerRequest:
    business_id: BusinessId
    brief_id: BriefId
    source_signal_id: SignalId | None
    template_name: str
    ad_copy: AdCopy
    brand_kit: BrandKit
    background_asset_id: AssetRef | None
    destination_url: str | None
    formats: Sequence[Format]


class ComposeBanner:
    def __init__(
        self, banner_composer: BannerComposerPort, assets: CreativeAssetRepository
    ) -> None:
        self._banner_composer = banner_composer
        self._assets = assets

    async def execute(self, request: ComposeBannerRequest) -> Sequence[AssetId]:
        spec = BannerSpec(
            template_name=request.template_name,
            ad_copy=request.ad_copy,
            brand_kit=request.brand_kit,
            background_image=request.background_asset_id,
            formats=request.formats,
        )
        rendered_assets = await self._banner_composer.compose(spec)
        created_ids: list[AssetId] = []
        for rendered in rendered_assets:
            asset = self._to_creative_asset(request, rendered)
            await self._assets.add(asset)
            created_ids.append(asset.asset_id)
        return created_ids

    def _to_creative_asset(
        self, request: ComposeBannerRequest, rendered: RenderedAsset
    ) -> CreativeAsset:
        provenance = Provenance(
            renderer_used=_BANNER_COMPOSER_NAME,
            model_name=MODEL_NAME_BY_RENDERER[_BANNER_COMPOSER_NAME],
            seed=None,
            brief_id=request.brief_id,
            source_signal_id=request.source_signal_id,
            generation_status=self._generation_status(request),
            generated_at=rendered.generated_at,
        )
        return CreativeAsset(
            asset_id=AssetId.new(),
            business_id=request.business_id,
            media_kind=rendered.media_kind,
            format=rendered.format,
            duration_seconds=None,
            storage_uri=rendered.storage_uri,
            checksum=rendered.checksum,
            cost_estimate=rendered.cost_estimate,
            provenance=provenance,
            ad_copy=request.ad_copy,
            destination_url=request.destination_url,
        )

    def _generation_status(self, request: ComposeBannerRequest) -> GenerationStatus:
        """Sin `background_image` no hay ningun visual generado por modelo
        debajo del copy — sea porque el brief nunca pidio uno, sea porque
        la generacion no estuvo disponible: en los dos casos es honesto
        marcarlo `COMPOSER_ONLY_FALLBACK` (correccion del propietario
        2026-09-09: "label it as such in the asset's provenance")."""
        if request.background_asset_id is None:
            return GenerationStatus.COMPOSER_ONLY_FALLBACK
        return GenerationStatus.MODEL_GENERATED
