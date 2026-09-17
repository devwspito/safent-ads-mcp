"""`BrandAsset`: campos obligatorios y clasificacion de logos."""

from __future__ import annotations

import pytest

from safent_ads.brand.domain.brand_asset import AssetKind, BrandAsset
from safent_ads.brand.domain.errors import BlankFieldError


def test_rejects_blank_asset_id() -> None:
    with pytest.raises(BlankFieldError):
        BrandAsset(asset_id=" ", kind=AssetKind.ICON, storage_uri="s3://x", usage_rule="x")


def test_rejects_blank_storage_uri() -> None:
    with pytest.raises(BlankFieldError):
        BrandAsset(asset_id="icon-1", kind=AssetKind.ICON, storage_uri=" ", usage_rule="x")


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (AssetKind.LOGO_VECTOR, True),
        (AssetKind.LOGO_RASTER, True),
        (AssetKind.REFERENCE_PHOTO, False),
        (AssetKind.ICON, False),
    ],
)
def test_is_logo(kind: AssetKind, expected: bool) -> None:
    asset = BrandAsset(asset_id="a-1", kind=kind, storage_uri="s3://x", usage_rule="x")

    assert asset.is_logo() is expected
