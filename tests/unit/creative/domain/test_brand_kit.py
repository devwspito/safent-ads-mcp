"""`SafeArea`/`BrandKit`: el area segura se valida al construir, nunca
despues del render (creative-port.md §"Reglas invariables")."""

from __future__ import annotations

import pytest

from safent_ads.creative.domain.brand_kit import (
    BrandKit,
    InvalidHexColorError,
    InvalidSafeAreaError,
    SafeArea,
)
from safent_ads.creative.domain.identifiers import AssetId


def test_safe_area_none_has_zero_margins() -> None:
    area = SafeArea.none()

    assert (area.top, area.bottom, area.left, area.right) == (0.0, 0.0, 0.0, 0.0)


def test_safe_area_rejects_margin_above_half() -> None:
    with pytest.raises(InvalidSafeAreaError):
        SafeArea(top=0.6, bottom=0.0, left=0.0, right=0.0)


def test_safe_area_rejects_negative_margin() -> None:
    with pytest.raises(InvalidSafeAreaError):
        SafeArea(top=-0.1, bottom=0.0, left=0.0, right=0.0)


def test_safe_area_rejects_opposite_margins_that_consume_all_space() -> None:
    with pytest.raises(InvalidSafeAreaError):
        SafeArea(top=0.5, bottom=0.5, left=0.0, right=0.0)


def test_brand_kit_rejects_invalid_hex_color() -> None:
    with pytest.raises(InvalidHexColorError):
        BrandKit(
            primary_font="Inter",
            secondary_font="Inter",
            primary_color_hex="not-a-color",
            secondary_color_hex="#FFFFFF",
            logo_asset_id=AssetId.new(),
            safe_area=SafeArea.none(),
        )


def test_brand_kit_accepts_valid_hex_colors() -> None:
    kit = BrandKit(
        primary_font="Inter",
        secondary_font="Inter",
        primary_color_hex="#1A2B3C",
        secondary_color_hex="#ffffff",
        logo_asset_id=AssetId.new(),
        safe_area=SafeArea.none(),
    )

    assert kit.primary_color_hex == "#1A2B3C"
