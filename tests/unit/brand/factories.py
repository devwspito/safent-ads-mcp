"""Fabrica de `BrandKit` valido para los tests de `brand` (domain,
application, infrastructure): construir el agregado a mano en cada test
seria repetitivo, dado el numero de campos obligatorios."""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.brand.domain.brand_asset import AssetKind, BrandAsset
from safent_ads.brand.domain.brand_kit import BrandKit
from safent_ads.brand.domain.claims_policy import normalize_forbidden_claims
from safent_ads.brand.domain.color_palette import ColorPalette, ColorRole, ColorSwatch
from safent_ads.brand.domain.identifiers import BrandKitId
from safent_ads.brand.domain.legal_disclaimer import LegalDisclaimer
from safent_ads.brand.domain.tone_of_voice import ToneOfVoice
from safent_ads.brand.domain.typography import Typography
from safent_ads.shared.ids import BusinessId

FIXED_UPDATED_AT = datetime(2026, 9, 9, 10, 0, tzinfo=UTC)


def make_brand_kit(**overrides: object) -> BrandKit:
    defaults: dict[str, object] = {
        "brand_kit_id": BrandKitId.new(),
        "business_id": BusinessId.new(),
        "typography": Typography(
            primary_family="Fake Sans", licence_note="Google Fonts, SIL OFL 1.1"
        ),
        "palette": ColorPalette(
            swatches=(
                ColorSwatch(role=ColorRole.PRIMARY, hex="#101010", contrast_ratio_on_white=18.0),
            )
        ),
        "tone_of_voice": ToneOfVoice(description="Cercano y claro."),
        "updated_at": FIXED_UPDATED_AT,
        "assets": (
            BrandAsset(
                asset_id="logo-principal",
                kind=AssetKind.LOGO_VECTOR,
                storage_uri="s3://brand/example/logo.svg",
                usage_rule="no deformar",
            ),
        ),
        "forbidden_claims": normalize_forbidden_claims(()),
        "legal_disclaimers": (
            LegalDisclaimer(text="Resultados de campañas anteriores, no garantia futura."),
        ),
    }
    defaults.update(overrides)
    return BrandKit(**defaults)  # type: ignore[arg-type]


__all__ = ["FIXED_UPDATED_AT", "make_brand_kit"]
