from __future__ import annotations

import pytest

from safent_ads.creative.domain.asset_spec import AssetSpec, AssetSpecError
from safent_ads.creative.domain.brand_kit import SafeArea
from safent_ads.creative.domain.enums import AssetKind, Format, VideoDurationSeconds


def test_video_requires_duration() -> None:
    with pytest.raises(AssetSpecError):
        AssetSpec(kind=AssetKind.VIDEO, format=Format.STORY_1080X1920, safe_area=SafeArea.none())


def test_non_video_rejects_duration() -> None:
    with pytest.raises(AssetSpecError):
        AssetSpec(
            kind=AssetKind.IMAGE,
            format=Format.SQUARE_1080,
            safe_area=SafeArea.none(),
            duration=VideoDurationSeconds.SIX,
        )


def test_valid_video_spec() -> None:
    spec = AssetSpec(
        kind=AssetKind.VIDEO,
        format=Format.STORY_1080X1920,
        safe_area=SafeArea.reels_default(),
        duration=VideoDurationSeconds.TEN,
    )

    assert spec.duration is not None
    assert spec.duration.seconds == 10


@pytest.mark.parametrize(
    "fmt",
    list(Format),
)
def test_all_seven_formats_are_registered(fmt: Format) -> None:
    assert fmt.width > 0
    assert fmt.height > 0
