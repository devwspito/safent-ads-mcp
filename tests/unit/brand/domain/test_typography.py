"""`Typography`: familia y nota de licencia obligatorias."""

from __future__ import annotations

import pytest

from safent_ads.brand.domain.errors import BlankFieldError
from safent_ads.brand.domain.typography import Typography


def test_rejects_blank_primary_family() -> None:
    with pytest.raises(BlankFieldError):
        Typography(primary_family="  ", licence_note="SIL OFL")


def test_rejects_blank_licence_note() -> None:
    with pytest.raises(BlankFieldError):
        Typography(primary_family="Fake Sans", licence_note="  ")


def test_accepts_minimal_valid_typography() -> None:
    typography = Typography(primary_family="Fake Sans", licence_note="SIL OFL 1.1")

    assert typography.secondary_family is None
    assert typography.weights == ()
