"""`crop_to_format`: geometria pura, generica para cualquier tamano de
activo fuente (correccion del propietario 2026-09-09: no atada a los 3
tamanos de un backend concreto). Los ejemplos verifican a mano la
aritmetica de recorte desde un origen 1024x1536 (portrait) o 1536x1024
(landscape) — tamanos elegidos solo como ejemplo, no como contrato."""

from __future__ import annotations

from safent_ads.creative.domain.brand_kit import SafeArea
from safent_ads.creative.domain.enums import Format
from safent_ads.creative.domain.image_crop import CropBox, crop_to_format


def test_crop_portrait_source_to_story_9x16_trims_width() -> None:
    box = crop_to_format(1024, 1536, Format.STORY_1080X1920)

    assert box == CropBox(left=80, top=0, width=864, height=1536)


def test_crop_portrait_source_to_feed_4x5_trims_height() -> None:
    box = crop_to_format(1024, 1536, Format.PORTRAIT_FEED_1080X1350)

    assert box == CropBox(left=0, top=128, width=1024, height=1280)


def test_crop_portrait_source_to_square_trims_height() -> None:
    box = crop_to_format(1024, 1536, Format.SQUARE_1080)

    assert box == CropBox(left=0, top=256, width=1024, height=1024)


def test_crop_landscape_source_to_link_1_91_trims_height() -> None:
    box = crop_to_format(1536, 1024, Format.LINK_1200X628)

    assert box.left == 0
    assert box.width == 1536
    assert box.height == 804
    assert box.top == (1024 - 804) // 2


def test_crop_landscape_source_to_medium_rectangle_trims_width() -> None:
    box = crop_to_format(1536, 1024, Format.MEDIUM_RECTANGLE_300X250)

    assert box.top == 0
    assert box.height == 1024
    assert box.width == 1229
    assert box.left == (1536 - 1229) // 2


def test_crop_exact_match_returns_full_source() -> None:
    box = crop_to_format(1080, 1080, Format.SQUARE_1080)

    assert box == CropBox(left=0, top=0, width=1080, height=1080)


def test_crop_never_exceeds_source_dimensions() -> None:
    for source_width, source_height in ((100, 100), (4000, 3000), (768, 1365)):
        for target in Format:
            if not target.is_renderer_eligible:
                continue
            box = crop_to_format(source_width, source_height, target)
            assert 0 < box.width <= source_width
            assert 0 < box.height <= source_height
            assert box.left + box.width <= source_width
            assert box.top + box.height <= source_height


def test_safe_area_with_more_top_margin_shifts_crop_downward() -> None:
    heavy_top_margin = SafeArea(top=0.2, bottom=0.05, left=0.0, right=0.0)

    box = crop_to_format(1024, 1536, Format.SQUARE_1080, safe_area=heavy_top_margin)

    assert box.top == 1536 - 1024


def test_safe_area_with_more_bottom_margin_shifts_crop_upward() -> None:
    heavy_bottom_margin = SafeArea(top=0.05, bottom=0.2, left=0.0, right=0.0)

    box = crop_to_format(1024, 1536, Format.SQUARE_1080, safe_area=heavy_bottom_margin)

    assert box.top == 0


def test_safe_area_with_symmetric_margins_centers_crop() -> None:
    symmetric = SafeArea(top=0.1, bottom=0.1, left=0.0, right=0.0)

    box = crop_to_format(1024, 1536, Format.SQUARE_1080, safe_area=symmetric)

    assert box.top == (1536 - 1024) // 2
