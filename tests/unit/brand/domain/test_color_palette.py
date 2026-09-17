"""`ColorSwatch`: formato `#RRGGBB`, rango de contraste y umbral WCAG AA."""

from __future__ import annotations

import pytest

from safent_ads.brand.domain.color_palette import ColorPalette, ColorRole, ColorSwatch
from safent_ads.brand.domain.errors import InvalidContrastRatioError, InvalidHexColorError


def test_rejects_invalid_hex() -> None:
    with pytest.raises(InvalidHexColorError):
        ColorSwatch(role=ColorRole.PRIMARY, hex="not-a-color", contrast_ratio_on_white=10.0)


def test_rejects_contrast_ratio_out_of_range() -> None:
    with pytest.raises(InvalidContrastRatioError):
        ColorSwatch(role=ColorRole.PRIMARY, hex="#000000", contrast_ratio_on_white=25.0)


def test_meets_wcag_aa_normal_text_at_threshold() -> None:
    swatch = ColorSwatch(role=ColorRole.TEXT, hex="#000000", contrast_ratio_on_white=4.5)

    assert swatch.meets_wcag_aa_normal_text() is True


def test_below_wcag_aa_threshold_fails() -> None:
    swatch = ColorSwatch(role=ColorRole.TEXT, hex="#cccccc", contrast_ratio_on_white=1.2)

    assert swatch.meets_wcag_aa_normal_text() is False


def test_swatch_for_returns_none_when_role_missing() -> None:
    palette = ColorPalette(
        swatches=(ColorSwatch(role=ColorRole.PRIMARY, hex="#000000", contrast_ratio_on_white=21.0),)
    )

    assert palette.swatch_for(ColorRole.ACCENT) is None
    assert palette.swatch_for(ColorRole.PRIMARY) is not None
