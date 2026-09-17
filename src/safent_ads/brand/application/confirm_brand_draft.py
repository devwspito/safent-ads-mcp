"""`ConfirmBrandDraft`: caso de uso que respalda la herramienta MCP
`confirm_brand_draft` y `POST /api/v1/brand/confirm` -- el UNICO camino
para que `is_confirmed` pase a `True` (owner request: un rastreo
automatico "nunca se usa como fuente de verdad hasta que el propietario
lo confirma"). Mismo patron de reemplazo entero que
`brand.domain.brand_kit` documenta ("el propietario reemplaza el YAML
completo en vez de editar campo a campo"): typography/palette/tono/assets
vienen integros de `request`, nunca se fusionan campo a campo con el kit
previo. Solo se conservan del kit previo la identidad
(`brand_kit_id`) y los bloques que este caso de uso no gestiona
(`claims_allowlist`, `forbidden_claims`, avisos legales, restricciones de
plataforma -- eso sigue siendo terreno del YAML/`SqlBrandKitRepository`)."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.brand.application.errors import (
    BrandDraftAssetNotFoundError,
    BrandDraftNotFoundError,
)
from safent_ads.brand.application.ports import BrandDiscoveryDraftRepository, BrandKitRepository
from safent_ads.brand.domain.brand_asset import BrandAsset
from safent_ads.brand.domain.brand_kit import BrandKit
from safent_ads.brand.domain.claims_policy import normalize_forbidden_claims
from safent_ads.brand.domain.color_palette import ColorPalette, ColorSwatch
from safent_ads.brand.domain.discovery import BrandDiscoveryDraft
from safent_ads.brand.domain.identifiers import BrandKitId
from safent_ads.brand.domain.tone_of_voice import ToneOfVoice
from safent_ads.brand.domain.typography import Typography
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId

_CONFIRMED_USAGE_RULE = "Confirmado por el propietario tras revisar el borrador de rastreo."


@dataclass(frozen=True, kw_only=True)
class ConfirmBrandDraftRequest:
    business_id: BusinessId
    primary_font: str
    font_licence_note: str
    tone_description: str
    secondary_font: str | None = None
    font_weights: tuple[str, ...] = ()
    palette: tuple[ColorSwatch, ...] = ()
    tone_adjectives: tuple[str, ...] = ()
    tone_avoid: tuple[str, ...] = ()
    selected_asset_ids: tuple[str, ...] = ()


class ConfirmBrandDraft:
    def __init__(
        self,
        *,
        drafts: BrandDiscoveryDraftRepository,
        brand_kits: BrandKitRepository,
        clock: Clock,
    ) -> None:
        self._drafts = drafts
        self._brand_kits = brand_kits
        self._clock = clock

    async def execute(self, request: ConfirmBrandDraftRequest) -> BrandKit:
        draft = await self._drafts.get_by_business(request.business_id)
        if draft is None:
            raise BrandDraftNotFoundError(
                f"sin borrador de marca para el negocio {request.business_id}"
            )
        assets = self._resolve_selected_assets(draft, request.selected_asset_ids)
        existing = await self._brand_kits.get_by_business(request.business_id)
        kit = self._build_confirmed_kit(request, existing, assets)
        await self._brand_kits.save(kit)
        return kit

    def _resolve_selected_assets(
        self, draft: BrandDiscoveryDraft, asset_ids: tuple[str, ...]
    ) -> tuple[BrandAsset, ...]:
        assets = []
        for asset_id in asset_ids:
            candidate = draft.logo_by_asset_id(asset_id)
            if candidate is None:
                raise BrandDraftAssetNotFoundError(
                    f"asset_id {asset_id!r} no esta en el borrador actual"
                )
            assets.append(candidate.to_brand_asset(usage_rule=_CONFIRMED_USAGE_RULE))
        return tuple(assets)

    def _build_confirmed_kit(
        self,
        request: ConfirmBrandDraftRequest,
        existing: BrandKit | None,
        assets: tuple[BrandAsset, ...],
    ) -> BrandKit:
        return BrandKit(
            brand_kit_id=existing.brand_kit_id if existing else BrandKitId.new(),
            business_id=request.business_id,
            typography=Typography(
                primary_family=request.primary_font,
                secondary_family=request.secondary_font,
                licence_note=request.font_licence_note,
                weights=request.font_weights,
            ),
            palette=ColorPalette(swatches=request.palette),
            tone_of_voice=ToneOfVoice(
                description=request.tone_description,
                adjectives=request.tone_adjectives,
                avoid=request.tone_avoid,
            ),
            updated_at=self._clock.now(),
            assets=assets,
            claims_allowlist=existing.claims_allowlist if existing else frozenset(),
            forbidden_claims=normalize_forbidden_claims(
                existing.forbidden_claims if existing else ()
            ),
            legal_disclaimers=existing.legal_disclaimers if existing else (),
            platform_constraints=existing.platform_constraints if existing else (),
            is_confirmed=True,
        )
