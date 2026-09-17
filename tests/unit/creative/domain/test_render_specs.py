"""`ImageSpec`/`VideoSpec`/`Timeline`/`BannerSpec` exactos de
`creative-port.md §"Tipos del dominio"` (mas `Clip`/`Subtitle`/`AudioAsset`
inferidos, ver docstring del modulo)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.creative.domain.enums import Format, Language, MediaKind, RendererName
from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.render_specs import (
    AudioAsset,
    Clip,
    ImageSpec,
    MusicSpec,
    RenderedAsset,
    RenderSpecError,
    Subtitle,
    Timeline,
    VideoSpec,
    VoiceSpec,
)
from safent_ads.creative.domain.storage import StorageUri
from tests.unit.creative.domain.factories import make_brand_kit


def test_image_spec_rejects_empty_prompt() -> None:
    with pytest.raises(RenderSpecError):
        ImageSpec(
            prompt="   ",
            reference_assets=(),
            format=Format.SQUARE_1080,
            seed=None,
            brand_kit=make_brand_kit(),
        )


def test_video_spec_rejects_non_positive_duration() -> None:
    with pytest.raises(RenderSpecError):
        VideoSpec(
            key_frames=(),
            motion_prompt="camara lenta",
            duration_s=0,
            format=Format.STORY_1080X1920,
            seed=1,
        )


def test_image_spec_rejects_leaderboard_728x90() -> None:
    with pytest.raises(RenderSpecError, match="728x90"):
        ImageSpec(
            prompt="escena de estudio",
            reference_assets=(),
            format=Format.LEADERBOARD_728X90,
            seed=None,
            brand_kit=make_brand_kit(),
        )


def test_image_spec_rejects_mobile_leaderboard_320x50() -> None:
    with pytest.raises(RenderSpecError, match="320x50"):
        ImageSpec(
            prompt="escena de estudio",
            reference_assets=(),
            format=Format.MOBILE_LEADERBOARD_320X50,
            seed=None,
            brand_kit=make_brand_kit(),
        )


def test_video_spec_rejects_leaderboard_728x90() -> None:
    with pytest.raises(RenderSpecError, match="728x90"):
        VideoSpec(
            key_frames=(),
            motion_prompt="camara lenta",
            duration_s=6,
            format=Format.LEADERBOARD_728X90,
            seed=1,
        )


def test_subtitle_rejects_end_before_start() -> None:
    with pytest.raises(RenderSpecError):
        Subtitle(text="hola", start_s=2.0, end_s=1.0)


def test_clip_rejects_end_before_start() -> None:
    with pytest.raises(RenderSpecError):
        Clip(source=AssetId.new(), start_s=2.0, end_s=1.0)


def test_timeline_requires_at_least_one_clip() -> None:
    with pytest.raises(RenderSpecError):
        Timeline(
            clips=(),
            voiceover=None,
            music=None,
            subtitles=(),
            safe_area=make_brand_kit().safe_area,
            exports=(Format.STORY_1080X1920,),
        )


def test_timeline_requires_at_least_one_export() -> None:
    clip = Clip(source=AssetId.new(), start_s=0.0, end_s=3.0)

    with pytest.raises(RenderSpecError):
        Timeline(
            clips=(clip,),
            voiceover=None,
            music=None,
            subtitles=(),
            safe_area=make_brand_kit().safe_area,
            exports=(),
        )


def test_valid_timeline_with_audio() -> None:
    clip = Clip(source=AssetId.new(), start_s=0.0, end_s=3.0)
    voiceover = AudioAsset(
        storage_uri=StorageUri("audio/vo.mp3"),
        duration_s=3.0,
        renderer_used=RendererName.CHATTERBOX_ES_ES,
    )

    timeline = Timeline(
        clips=(clip,),
        voiceover=voiceover,
        music=None,
        subtitles=(Subtitle(text="Hola", start_s=0.0, end_s=1.0),),
        safe_area=make_brand_kit().safe_area,
        exports=(Format.STORY_1080X1920,),
    )

    assert timeline.voiceover is voiceover


def test_voice_spec_rejects_empty_text() -> None:
    with pytest.raises(RenderSpecError):
        VoiceSpec(text="  ", language=Language.ES_ES)


def test_voice_spec_rejects_speed_out_of_range() -> None:
    with pytest.raises(RenderSpecError):
        VoiceSpec(text="Hola", language=Language.ES_ES, speed=3.0)


def test_music_spec_rejects_non_positive_duration() -> None:
    with pytest.raises(RenderSpecError):
        MusicSpec(mood_prompt="animado", duration_s=0)


def test_rendered_asset_carries_provenance() -> None:
    asset = RenderedAsset(
        storage_uri=StorageUri("img/x.png"),
        media_kind=MediaKind.IMAGE,
        format=Format.SQUARE_1080,
        checksum="a" * 64,
        renderer_used=RendererName.QWEN_IMAGE_2512,
        cost_estimate=Money.zero("EUR"),
        duration_s=None,
        generated_at=datetime.now(UTC),
    )

    assert asset.renderer_used == RendererName.QWEN_IMAGE_2512
