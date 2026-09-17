"""`BrandReadPort` real (integracion) sobre `brand_kits`
(`0014_brand.py`): mismo agregado y misma traduccion campo a campo que
`brand.presentation.serializers` (REST), pero a los DTOs tipados de esta
lane (`mcp.application.dto`) en vez de `dict[str, object]`. Una sesion por
llamada, mismo patron que `sql_business_directory.SqlBusinessDirectory`."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.brand.domain.brand_asset import AssetKind, BrandAsset
from safent_ads.brand.infrastructure.sql_brand_kit_repository import SqlBrandKitRepository
from safent_ads.mcp.application.dto import (
    BrandAssetSummary,
    BrandKitDetail,
    ColorSwatchDetail,
    LegalDisclaimerDetail,
    PlatformConstraintDetail,
    ToneOfVoiceDetail,
    TypographyDetail,
)
from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.shared.ids import BusinessId


class SqlBrandReadPort:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_brand_kit(self, business_id: str) -> BrandKitDetail:
        async with self._session_factory() as session:
            brand_kit = await SqlBrandKitRepository(session).get_by_business(
                BusinessId.parse(business_id)
            )
        if brand_kit is None:
            raise EntityNotFoundError(f"kit de marca aun no disponible para {business_id}")
        return BrandKitDetail(
            brand_kit_id=str(brand_kit.brand_kit_id),
            business_id=str(brand_kit.business_id),
            typography=TypographyDetail(
                primary_family=brand_kit.typography.primary_family,
                secondary_family=brand_kit.typography.secondary_family,
                licence_note=brand_kit.typography.licence_note,
                weights=list(brand_kit.typography.weights),
            ),
            palette=[
                ColorSwatchDetail(
                    role=swatch.role.value,
                    hex=swatch.hex,
                    contrast_ratio_on_white=swatch.contrast_ratio_on_white,
                    meets_wcag_aa_normal_text=swatch.meets_wcag_aa_normal_text(),
                )
                for swatch in brand_kit.palette.swatches
            ],
            tone_of_voice=ToneOfVoiceDetail(
                description=brand_kit.tone_of_voice.description,
                adjectives=list(brand_kit.tone_of_voice.adjectives),
                avoid=list(brand_kit.tone_of_voice.avoid),
            ),
            assets=[_asset_summary(asset) for asset in brand_kit.assets],
            claims_allowlist=sorted(brand_kit.claims_allowlist),
            forbidden_claims=sorted(brand_kit.forbidden_claims),
            legal_disclaimers=[
                LegalDisclaimerDetail(
                    text=disclaimer.text,
                    applies_to=(
                        [p.value for p in disclaimer.applies_to]
                        if disclaimer.applies_to is not None
                        else None
                    ),
                )
                for disclaimer in brand_kit.legal_disclaimers
            ],
            platform_constraints=[
                PlatformConstraintDetail(
                    platform=constraint.platform.value,
                    max_headline_chars=constraint.max_headline_chars,
                    requires_disclaimer=constraint.requires_disclaimer,
                    notes=constraint.notes,
                )
                for constraint in brand_kit.platform_constraints
            ],
            is_complete=brand_kit.is_complete(),
            updated_at=brand_kit.updated_at,
        )

    async def list_brand_assets(
        self, business_id: str, *, kind: str | None
    ) -> list[BrandAssetSummary]:
        async with self._session_factory() as session:
            brand_kit = await SqlBrandKitRepository(session).get_by_business(
                BusinessId.parse(business_id)
            )
        if brand_kit is None:
            return []
        assets = brand_kit.assets_of_kind(AssetKind(kind) if kind else None)
        return [_asset_summary(asset) for asset in assets]


def _asset_summary(asset: BrandAsset) -> BrandAssetSummary:
    return BrandAssetSummary(
        asset_id=asset.asset_id,
        kind=asset.kind.value,
        url=asset.storage_uri,
        usage=asset.usage_rule,
    )
