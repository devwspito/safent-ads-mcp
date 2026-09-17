"""`BrandDiscoveryDraft`: validacion de URL, candidatos con confianza,
saneado de PII y `merge_into_kit` (owner request: rastreo de identidad de
marca desde un sitio web opcional)."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from safent_ads.brand.domain.brand_asset import AssetKind
from safent_ads.brand.domain.brand_kit import PLACEHOLDER_MARKER
from safent_ads.brand.domain.color_palette import ColorRole
from safent_ads.brand.domain.discovery import (
    _MAX_PII_SCAN_CHARS,
    BrandDiscoveryDraft,
    ColorCandidate,
    ContactChannelKind,
    CopySample,
    DiscoverySource,
    LogoCandidate,
    TypographyCandidate,
    contrast_ratio_on_white,
    detect_contact_channel_kinds,
    strip_pii,
    validate_discovery_url,
)
from safent_ads.brand.domain.errors import (
    BlankFieldError,
    InvalidConfidenceError,
    InvalidDiscoveryUrlError,
    InvalidHexColorError,
    PiiDetectedInCopySampleError,
)
from safent_ads.brand.domain.identifiers import BrandKitId
from safent_ads.shared.ids import BusinessId
from tests.unit.brand.factories import make_brand_kit

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _draft(**overrides: object) -> BrandDiscoveryDraft:
    defaults: dict[str, object] = {
        "business_id": BusinessId.new(),
        "source_url": "https://example-business.test",
        "discovered_at": _NOW,
    }
    defaults.update(overrides)
    return BrandDiscoveryDraft(**defaults)  # type: ignore[arg-type]


def _logo(
    asset_id: str = "logo-1",
    *,
    kind: AssetKind = AssetKind.LOGO_RASTER,
    confidence: float = 0.7,
    source: DiscoverySource = DiscoverySource.OG_IMAGE,
) -> LogoCandidate:
    return LogoCandidate(
        asset_id=asset_id,
        kind=kind,
        storage_uri=f"brand/{asset_id}.png",
        sha256="a" * 64,
        source=source,
        confidence=confidence,
    )


class TestValidateDiscoveryUrl:
    def test_accepts_https_public_host(self) -> None:
        assert validate_discovery_url("https://example-business.test/") == "example-business.test"

    def test_accepts_http_scheme(self) -> None:
        assert validate_discovery_url("http://example-business.test") == "example-business.test"

    def test_rejects_non_http_scheme(self) -> None:
        with pytest.raises(InvalidDiscoveryUrlError, match="esquema"):
            validate_discovery_url("ftp://example-business.test")

    def test_rejects_ip_literal_host(self) -> None:
        with pytest.raises(InvalidDiscoveryUrlError, match="literal IP"):
            validate_discovery_url("https://203.0.113.10/")

    def test_rejects_embedded_credentials(self) -> None:
        with pytest.raises(InvalidDiscoveryUrlError, match="credenciales"):
            validate_discovery_url("https://user:pass@example-business.test")

    def test_rejects_url_without_host(self) -> None:
        with pytest.raises(InvalidDiscoveryUrlError, match="sin host"):
            validate_discovery_url("https:///path")

    def test_accepts_explicit_standard_port(self) -> None:
        assert validate_discovery_url("https://example-business.test:443/") == (
            "example-business.test"
        )

    @pytest.mark.parametrize("port", [9200, 6379, 22, 3306, 8080])
    def test_rejects_non_standard_port(self, port: int) -> None:
        """F-11: `brand/domain/discovery.py:116-135` (revision original)
        aceptaba cualquier puerto -> sondeo de servicios internos
        (`:9200` Elasticsearch, `:6379` Redis...). Solo 80/443."""
        with pytest.raises(InvalidDiscoveryUrlError, match="puerto"):
            validate_discovery_url(f"https://example-business.test:{port}/")


class TestPiiHelpers:
    def test_strip_pii_removes_email(self) -> None:
        assert "@" not in strip_pii("Escribenos a hola@example-business.test hoy mismo")

    def test_strip_pii_removes_phone_like_digit_runs(self) -> None:
        cleaned = strip_pii("Llama al 912 345 678 ahora")
        assert "912" not in cleaned

    def test_strip_pii_keeps_short_digit_runs(self) -> None:
        assert strip_pii("Programa 2026-2027 abierto") == "Programa 2026-2027 abierto"

    def test_strip_pii_removes_dni(self) -> None:
        cleaned = strip_pii("Titular del contrato: 12345678Z")
        assert "12345678Z" not in cleaned

    def test_strip_pii_removes_nie(self) -> None:
        cleaned = strip_pii("Extranjero con NIE X1234567L registrado")
        assert "X1234567L" not in cleaned

    def test_strip_pii_removes_iban(self) -> None:
        cleaned = strip_pii("Ingresa el pago en ES91 2100 0418 4502 0005 1332")
        assert "ES91" not in cleaned
        assert "2100" not in cleaned

    def test_strip_pii_keeps_unrelated_alphanumeric_codes(self) -> None:
        """Un codigo de programa corriente (letras+digitos cortos) no debe
        confundirse con DNI/NIE/IBAN."""
        assert strip_pii("Programa ref. AB12 disponible") == "Programa ref. AB12 disponible"

    def test_detect_contact_channel_kinds_finds_email_and_phone(self) -> None:
        kinds = detect_contact_channel_kinds("Contacto: hola@example-business.test o 912345678")

        assert ContactChannelKind.EMAIL in kinds
        assert ContactChannelKind.PHONE in kinds

    def test_contact_channel_scan_stays_under_1s_on_2mib_page(self) -> None:
        """F-1 (CWE-1333/CWE-400): el patron original era cuadratico sobre
        una racha larga de `[\\w.+-]` sin `@` -- *medido* en la revision,
        120 KB -> 11,6 s de CPU. 2 MiB del mismo relleno hostil debe
        resolverse en milisegundos, no minutos, con margen generoso sobre
        el limite de 50 ms exigido."""
        hostile_page = "a.-+" * (2 * 1024 * 1024 // 4)

        started_at = time.perf_counter()
        kinds = detect_contact_channel_kinds(hostile_page)
        elapsed_s = time.perf_counter() - started_at

        assert kinds == frozenset()
        assert elapsed_s < 0.05

    def test_strip_pii_never_lets_a_full_email_or_phone_survive_past_the_scan_cap(self) -> None:
        """La segunda capa de defensa (F-1): incluso si el llamante no
        recorta antes, `strip_pii` nunca deja pasar un email/telefono
        completo mas alla de `_MAX_PII_SCAN_CHARS`."""
        padding = "x" * (_MAX_PII_SCAN_CHARS + 1000)
        cleaned = strip_pii(f"{padding} hola@example-business.test")

        assert "@" not in cleaned

    def test_detect_contact_channel_kinds_empty_for_clean_text(self) -> None:
        assert detect_contact_channel_kinds("Servicio profesional de calidad") == frozenset()


class TestContrastRatioOnWhite:
    def test_white_on_white_is_one(self) -> None:
        assert contrast_ratio_on_white("#FFFFFF") == 1.0

    def test_black_on_white_is_twentyone(self) -> None:
        assert contrast_ratio_on_white("#000000") == 21.0


class TestCandidateInvariants:
    def test_logo_candidate_rejects_confidence_out_of_range(self) -> None:
        with pytest.raises(InvalidConfidenceError):
            _logo(confidence=1.5)

    def test_logo_candidate_rejects_blank_asset_id(self) -> None:
        with pytest.raises(BlankFieldError):
            LogoCandidate(
                asset_id="  ",
                kind=AssetKind.ICON,
                storage_uri="brand/x.png",
                sha256="a" * 64,
                source=DiscoverySource.FAVICON,
                confidence=0.3,
            )

    def test_color_candidate_rejects_invalid_hex(self) -> None:
        with pytest.raises(InvalidHexColorError):
            ColorCandidate(
                hex="not-a-color", source=DiscoverySource.CSS_MOST_USED_COLOR, confidence=0.4
            )

    def test_typography_candidate_rejects_blank_family(self) -> None:
        with pytest.raises(BlankFieldError):
            TypographyCandidate(family=" ", source=DiscoverySource.CSS_FONT_FAMILY, confidence=0.4)

    def test_copy_sample_rejects_blank_text(self) -> None:
        with pytest.raises(BlankFieldError):
            CopySample(source=DiscoverySource.HERO_HEADLINE, text=" ")

    def test_copy_sample_rejects_pii_as_defense_in_depth(self) -> None:
        with pytest.raises(PiiDetectedInCopySampleError):
            CopySample(source=DiscoverySource.HERO_HEADLINE, text="Llamanos al 912345678")

    def test_copy_sample_rejects_dni_as_defense_in_depth(self) -> None:
        with pytest.raises(PiiDetectedInCopySampleError):
            CopySample(source=DiscoverySource.HERO_HEADLINE, text="DNI del titular 12345678Z")

    def test_copy_sample_rejects_iban_as_defense_in_depth(self) -> None:
        with pytest.raises(PiiDetectedInCopySampleError):
            CopySample(
                source=DiscoverySource.HERO_HEADLINE,
                text="Domiciliacion ES9121000418450200051332",
            )

    def test_copy_sample_accepts_clean_text(self) -> None:
        sample = CopySample(source=DiscoverySource.HERO_HEADLINE, text="Prepara tu lanzamiento")

        assert sample.text == "Prepara tu lanzamiento"


class TestDraftLookups:
    def test_top_logo_picks_highest_confidence(self) -> None:
        draft = _draft(logo_candidates=(_logo("a", confidence=0.3), _logo("b", confidence=0.8)))

        assert draft.top_logo().asset_id == "b"

    def test_top_logo_filters_by_kind(self) -> None:
        draft = _draft(
            logo_candidates=(
                _logo("icon", kind=AssetKind.ICON, confidence=0.9),
                _logo("raster", kind=AssetKind.LOGO_RASTER, confidence=0.4),
            )
        )

        assert draft.top_logo(kind=AssetKind.LOGO_RASTER).asset_id == "raster"

    def test_with_manual_logo_deduplicates_by_asset_id(self) -> None:
        draft = _draft(logo_candidates=(_logo("a", confidence=0.2),))

        updated = draft.with_manual_logo(_logo("a", confidence=1.0))

        assert len(updated.logo_candidates) == 1
        assert updated.logo_candidates[0].confidence == 1.0

    def test_logo_by_asset_id_returns_none_when_missing(self) -> None:
        assert _draft().logo_by_asset_id("missing") is None


class TestMergeIntoKit:
    def test_produces_unconfirmed_kit_with_no_existing_kit(self) -> None:
        draft = _draft()

        kit = draft.merge_into_kit(brand_kit_id=BrandKitId.new(), existing=None, now=_NOW)

        assert kit.is_confirmed is False
        assert kit.typography.primary_family == PLACEHOLDER_MARKER

    def test_fills_typography_from_top_candidate_when_no_existing_kit(self) -> None:
        draft = _draft(
            typography_candidates=(
                TypographyCandidate(
                    family="Poppins", source=DiscoverySource.CSS_FONT_FAMILY, confidence=0.6
                ),
            )
        )

        kit = draft.merge_into_kit(brand_kit_id=BrandKitId.new(), existing=None, now=_NOW)

        assert kit.typography.primary_family == "Poppins"
        assert kit.is_confirmed is False

    def test_builds_palette_from_color_candidates(self) -> None:
        draft = _draft(
            color_candidates=(
                ColorCandidate(
                    hex="#112233",
                    source=DiscoverySource.CSS_CUSTOM_PROPERTY,
                    confidence=0.9,
                    role_hint=ColorRole.PRIMARY,
                ),
            )
        )

        kit = draft.merge_into_kit(brand_kit_id=BrandKitId.new(), existing=None, now=_NOW)

        assert kit.palette.swatch_for(ColorRole.PRIMARY).hex == "#112233"

    def test_preserves_existing_confirmed_typography_instead_of_overwriting(self) -> None:
        existing = make_brand_kit()
        draft = _draft(
            typography_candidates=(
                TypographyCandidate(
                    family="Scraped Font", source=DiscoverySource.CSS_FONT_FAMILY, confidence=0.9
                ),
            )
        )

        kit = draft.merge_into_kit(brand_kit_id=existing.brand_kit_id, existing=existing, now=_NOW)

        assert kit.typography.primary_family == existing.typography.primary_family
        assert kit.is_confirmed is False

    def test_adds_at_most_one_logo_and_one_icon_to_preview_assets(self) -> None:
        draft = _draft(
            logo_candidates=(
                _logo("raster-1", kind=AssetKind.LOGO_RASTER, confidence=0.5),
                _logo("raster-2", kind=AssetKind.LOGO_RASTER, confidence=0.9),
                _logo("icon-1", kind=AssetKind.ICON, confidence=0.3),
            )
        )

        kit = draft.merge_into_kit(brand_kit_id=BrandKitId.new(), existing=None, now=_NOW)

        asset_ids = {a.asset_id for a in kit.assets}
        assert asset_ids == {"raster-2", "icon-1"}

    def test_reruns_stay_unconfirmed_even_if_kit_was_already_confirmed(self) -> None:
        existing = make_brand_kit(is_confirmed=True)
        draft = _draft()

        kit = draft.merge_into_kit(brand_kit_id=existing.brand_kit_id, existing=existing, now=_NOW)

        assert kit.is_confirmed is False
