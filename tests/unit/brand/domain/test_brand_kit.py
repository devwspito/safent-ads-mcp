"""`BrandKit`: suelo de reclamos prohibidos, completitud y deteccion de
reclamos (data-model.md ampliado por tool-surface.md §2.2/§6)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.brand.domain.brand_asset import AssetKind, BrandAsset
from safent_ads.brand.domain.brand_kit import PLACEHOLDER_MARKER
from safent_ads.brand.domain.claims_policy import DEFAULT_FORBIDDEN_CLAIMS
from safent_ads.brand.domain.color_palette import ColorPalette
from safent_ads.brand.domain.errors import (
    AllowedClaimConflictsWithForbiddenError,
    MissingBaselineForbiddenClaimsError,
)
from safent_ads.brand.domain.legal_disclaimer import LegalDisclaimer
from safent_ads.brand.domain.tone_of_voice import ToneOfVoice
from tests.unit.brand.factories import make_brand_kit


def test_construction_requires_baseline_forbidden_claims() -> None:
    with pytest.raises(MissingBaselineForbiddenClaimsError):
        make_brand_kit(forbidden_claims=frozenset({"cosa random"}))


def test_construction_accepts_baseline_forbidden_claims() -> None:
    kit = make_brand_kit(forbidden_claims=frozenset(DEFAULT_FORBIDDEN_CLAIMS))

    assert DEFAULT_FORBIDDEN_CLAIMS <= kit.forbidden_claims


@pytest.mark.parametrize(
    "text",
    [
        "Plazas garantizadas para todos",
        "Resultado GARANTIZADO o te devolvemos el dinero",
        "Exito asegurado si sigues el metodo",
    ],
)
def test_baseline_claims_are_detected_regardless_of_case(text: str) -> None:
    kit = make_brand_kit()

    assert kit.is_claim_forbidden(text)


def test_vertical_specific_claims_are_not_part_of_the_generic_baseline() -> None:
    """'plaza asegurada'/'100% aprobados' son ejemplo de vertical
    (sector regulado) en `config/brand/example.yaml`, no suelo de
    seguridad generico: un kit sin ese YAML no los bloquea."""
    kit = make_brand_kit()

    assert not kit.is_claim_forbidden("Plaza asegurada si estudias con nosotros")
    assert not kit.is_claim_forbidden("El 100% aprobados el año pasado")


def test_allowed_copy_is_not_flagged() -> None:
    kit = make_brand_kit()

    assert not kit.is_claim_forbidden("Clases en directo con profesorado especializado")


def test_logos_filters_by_kind() -> None:
    logo = BrandAsset(
        asset_id="logo-1", kind=AssetKind.LOGO_VECTOR, storage_uri="s3://logo.svg", usage_rule="x"
    )
    photo = BrandAsset(
        asset_id="photo-1",
        kind=AssetKind.REFERENCE_PHOTO,
        storage_uri="s3://photo.jpg",
        usage_rule="x",
    )
    kit = make_brand_kit(assets=(logo, photo))

    assert kit.logos() == (logo,)
    assert kit.assets_of_kind(AssetKind.REFERENCE_PHOTO) == (photo,)
    assert kit.assets_of_kind(None) == (logo, photo)


def test_asset_by_id_finds_a_matching_asset() -> None:
    logo = BrandAsset(
        asset_id="logo-1", kind=AssetKind.LOGO_VECTOR, storage_uri="s3://logo.svg", usage_rule="x"
    )
    kit = make_brand_kit(assets=(logo,))

    assert kit.asset_by_id("logo-1") is logo


def test_asset_by_id_returns_none_when_no_asset_matches() -> None:
    kit = make_brand_kit(assets=())

    assert kit.asset_by_id("missing") is None


def test_is_complete_false_without_assets() -> None:
    kit = make_brand_kit(assets=())

    assert kit.is_complete() is False


def test_is_complete_false_without_palette() -> None:
    kit = make_brand_kit(palette=ColorPalette())

    assert kit.is_complete() is False


def test_is_complete_false_with_placeholder_marker() -> None:
    kit = make_brand_kit(tone_of_voice=ToneOfVoice(description=f"{PLACEHOLDER_MARKER}: tono"))

    assert kit.is_complete() is False


def test_is_complete_true_when_fully_filled() -> None:
    kit = make_brand_kit()

    assert kit.is_complete() is True


def test_is_confirmed_defaults_to_true() -> None:
    kit = make_brand_kit()

    assert kit.is_confirmed is True


def test_is_complete_false_when_not_confirmed_even_if_otherwise_filled() -> None:
    kit = make_brand_kit(is_confirmed=False)

    assert kit.is_complete() is False


_REPLACE_CLAIMS_UPDATED_AT = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


def test_replace_claims_replaces_all_three_fields_entirely() -> None:
    kit = make_brand_kit(
        claims_allowlist=frozenset({"reclamo viejo"}),
        legal_disclaimers=(LegalDisclaimer(text="Aviso viejo"),),
    )

    updated = kit.replace_claims(
        claims_allowlist=("Envio gratis",),
        forbidden_claims=("mejor del mercado",),
        legal_disclaimers=(LegalDisclaimer(text="Aviso nuevo"),),
        updated_at=_REPLACE_CLAIMS_UPDATED_AT,
    )

    assert updated.claims_allowlist == frozenset({"Envio gratis"})
    assert "mejor del mercado" in updated.forbidden_claims
    assert DEFAULT_FORBIDDEN_CLAIMS <= updated.forbidden_claims
    assert updated.legal_disclaimers == (LegalDisclaimer(text="Aviso nuevo"),)
    assert updated.updated_at == _REPLACE_CLAIMS_UPDATED_AT


def test_replace_claims_preserves_identity_and_other_fields() -> None:
    kit = make_brand_kit()

    updated = kit.replace_claims(
        claims_allowlist=(),
        forbidden_claims=(),
        legal_disclaimers=(),
        updated_at=_REPLACE_CLAIMS_UPDATED_AT,
    )

    assert updated.brand_kit_id == kit.brand_kit_id
    assert updated.business_id == kit.business_id
    assert updated.typography == kit.typography
    assert updated.assets == kit.assets


def test_replace_claims_always_keeps_the_safety_floor_even_if_omitted() -> None:
    kit = make_brand_kit()

    updated = kit.replace_claims(
        claims_allowlist=(),
        forbidden_claims=(),
        legal_disclaimers=(),
        updated_at=_REPLACE_CLAIMS_UPDATED_AT,
    )

    assert DEFAULT_FORBIDDEN_CLAIMS <= updated.forbidden_claims


def test_replace_claims_rejects_an_allowed_claim_that_matches_a_forbidden_one() -> None:
    kit = make_brand_kit()

    with pytest.raises(AllowedClaimConflictsWithForbiddenError):
        kit.replace_claims(
            claims_allowlist=("mejor del mercado",),
            forbidden_claims=("Mejor del mercado",),
            legal_disclaimers=(),
            updated_at=_REPLACE_CLAIMS_UPDATED_AT,
        )


def test_replace_claims_rejects_an_allowed_claim_that_matches_the_safety_floor() -> None:
    kit = make_brand_kit()

    with pytest.raises(AllowedClaimConflictsWithForbiddenError):
        kit.replace_claims(
            claims_allowlist=("GARANTIZADO",),
            forbidden_claims=(),
            legal_disclaimers=(),
            updated_at=_REPLACE_CLAIMS_UPDATED_AT,
        )
