"""`ConfirmBrandDraft`: unico camino a `is_confirmed=True`; reemplaza
typography/palette/tono/assets enteros desde `request` (mismo patron que
`BrandKit` documenta), conserva identidad y bloques que no gestiona."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.brand.application.confirm_brand_draft import (
    ConfirmBrandDraft,
    ConfirmBrandDraftRequest,
)
from safent_ads.brand.application.errors import (
    BrandDraftAssetNotFoundError,
    BrandDraftNotFoundError,
)
from safent_ads.brand.domain.brand_asset import AssetKind
from safent_ads.brand.domain.claims_policy import normalize_forbidden_claims
from safent_ads.brand.domain.color_palette import ColorRole, ColorSwatch
from safent_ads.brand.domain.discovery import BrandDiscoveryDraft, DiscoverySource, LogoCandidate
from safent_ads.brand.domain.legal_disclaimer import LegalDisclaimer
from safent_ads.brand.testing.in_memory_brand_discovery_draft_repository import (
    InMemoryBrandDiscoveryDraftRepository,
)
from safent_ads.brand.testing.in_memory_brand_kit_repository import InMemoryBrandKitRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId
from tests.unit.brand.factories import make_brand_kit

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)

_LOGO = LogoCandidate(
    asset_id="logo-1",
    kind=AssetKind.LOGO_RASTER,
    storage_uri="brand/logo-1.png",
    sha256="a" * 64,
    source=DiscoverySource.OG_IMAGE,
    confidence=0.7,
)


def _base_request(business_id: BusinessId, **overrides: object) -> ConfirmBrandDraftRequest:
    defaults: dict[str, object] = {
        "business_id": business_id,
        "primary_font": "Poppins",
        "font_licence_note": "Google Fonts, SIL OFL 1.1",
        "tone_description": "Cercano y claro.",
        "palette": (
            ColorSwatch(role=ColorRole.PRIMARY, hex="#112233", contrast_ratio_on_white=10.0),
        ),
        "selected_asset_ids": ("logo-1",),
    }
    defaults.update(overrides)
    return ConfirmBrandDraftRequest(**defaults)  # type: ignore[arg-type]


def _use_case(
    *, draft: BrandDiscoveryDraft | None = None, existing_kit=None
) -> tuple[ConfirmBrandDraft, InMemoryBrandDiscoveryDraftRepository, InMemoryBrandKitRepository]:
    drafts = InMemoryBrandDiscoveryDraftRepository([draft] if draft else [])
    kits = InMemoryBrandKitRepository([existing_kit] if existing_kit else [])
    use_case = ConfirmBrandDraft(drafts=drafts, brand_kits=kits, clock=FixedClock(_NOW))
    return use_case, drafts, kits


async def test_raises_when_no_draft_exists() -> None:
    use_case, _drafts, _kits = _use_case()

    with pytest.raises(BrandDraftNotFoundError):
        await use_case.execute(_base_request(BusinessId.new()))


async def test_raises_when_selected_asset_id_is_not_in_the_draft() -> None:
    business_id = BusinessId.new()
    draft = BrandDiscoveryDraft(business_id=business_id, source_url=None, discovered_at=_NOW)
    use_case, _drafts, _kits = _use_case(draft=draft)

    with pytest.raises(BrandDraftAssetNotFoundError):
        await use_case.execute(_base_request(business_id))


async def test_confirming_produces_an_is_confirmed_kit() -> None:
    business_id = BusinessId.new()
    draft = BrandDiscoveryDraft(
        business_id=business_id, source_url=None, discovered_at=_NOW, logo_candidates=(_LOGO,)
    )
    use_case, _drafts, kits = _use_case(draft=draft)

    kit = await use_case.execute(_base_request(business_id))

    assert kit.is_confirmed is True
    assert kit.typography.primary_family == "Poppins"
    assert kit.palette.swatch_for(ColorRole.PRIMARY).hex == "#112233"
    assert kit.assets[0].storage_uri == "brand/logo-1.png"
    assert await kits.get_by_business(business_id) == kit


async def test_reuses_existing_brand_kit_id_and_unmanaged_blocks() -> None:
    business_id = BusinessId.new()
    draft = BrandDiscoveryDraft(
        business_id=business_id, source_url=None, discovered_at=_NOW, logo_candidates=(_LOGO,)
    )
    existing = make_brand_kit(
        business_id=business_id,
        forbidden_claims=normalize_forbidden_claims({"reclamo propio"}),
        legal_disclaimers=(LegalDisclaimer(text="Aviso legal propio"),),
    )
    use_case, _drafts, _kits = _use_case(draft=draft, existing_kit=existing)

    kit = await use_case.execute(_base_request(business_id))

    assert kit.brand_kit_id == existing.brand_kit_id
    assert kit.legal_disclaimers == existing.legal_disclaimers
    assert "reclamo propio" in kit.forbidden_claims


async def test_confirm_replaces_assets_entirely_rather_than_merging() -> None:
    business_id = BusinessId.new()
    draft = BrandDiscoveryDraft(
        business_id=business_id, source_url=None, discovered_at=_NOW, logo_candidates=(_LOGO,)
    )
    existing = make_brand_kit(business_id=business_id)
    use_case, _drafts, _kits = _use_case(draft=draft, existing_kit=existing)

    kit = await use_case.execute(_base_request(business_id))

    assert len(kit.assets) == 1
    assert kit.assets[0].asset_id == "logo-1"
