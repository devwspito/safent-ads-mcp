"""`MoviePyVideoComposer` con clips sinteticos diminutos (ffmpeg real,
sin red): T105 "unit test with tiny synthetic clips if ffmpeg present"."""

from __future__ import annotations

import asyncio
import io
import shutil
from pathlib import Path

import numpy as np
import pytest
from moviepy import AudioArrayClip
from PIL import Image

from safent_ads.creative.domain.brand_kit import SafeArea
from safent_ads.creative.domain.enums import Format, MediaKind, RendererName
from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.creative.domain.render_specs import AudioAsset, Clip, RenderedAsset, Timeline
from safent_ads.creative.domain.storage import StorageUri
from safent_ads.creative.infrastructure.video_composer import MoviePyVideoComposer
from tests.unit.creative.infrastructure.fakes import (
    FakeAssetRetrieval,
    FakeAssetStore,
    FakeCreativeAssetRepository,
    make_creative_asset,
)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg no disponible")

_FALLBACK_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")


def _tiny_png(color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (64, 64), color=color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _tiny_silence_mp3(tmp_path: Path, duration_s: float) -> bytes:
    samples = np.zeros((int(44100 * duration_s), 2), dtype=np.float32)
    clip = AudioArrayClip(samples, fps=44100)
    out = tmp_path / "silence.mp3"
    clip.write_audiofile(str(out), logger=None)
    return out.read_bytes()


def _composer(
    assets: FakeCreativeAssetRepository, retrieval: FakeAssetRetrieval
) -> MoviePyVideoComposer:
    return MoviePyVideoComposer(
        assets=assets,  # type: ignore[arg-type]
        asset_retrieval=retrieval,
        asset_store=FakeAssetStore(),
        brand_font_path=Path("/nonexistent/brand-font.ttf"),
        fallback_font_path=_FALLBACK_FONT,
    )


def test_assembles_two_image_clips_without_audio() -> None:
    async def _run() -> RenderedAsset:
        clip_a_id, clip_b_id = AssetId.new(), AssetId.new()
        uri_a, uri_b = StorageUri("image/a.png"), StorageUri("image/b.png")
        assets = FakeCreativeAssetRepository(
            {
                clip_a_id: make_creative_asset(
                    asset_id=clip_a_id, storage_uri=uri_a, media_kind=MediaKind.IMAGE
                ),
                clip_b_id: make_creative_asset(
                    asset_id=clip_b_id, storage_uri=uri_b, media_kind=MediaKind.IMAGE
                ),
            }
        )
        retrieval = FakeAssetRetrieval(
            {uri_a.key: _tiny_png((200, 30, 30)), uri_b.key: _tiny_png((30, 30, 200))}
        )
        timeline = Timeline(
            clips=(
                Clip(source=clip_a_id, start_s=0.0, end_s=0.3, on_screen_text="Hola"),
                Clip(source=clip_b_id, start_s=0.0, end_s=0.3),
            ),
            voiceover=None,
            music=None,
            subtitles=(),
            safe_area=SafeArea.none(),
            exports=(Format.SQUARE_1080,),
        )
        composer = _composer(assets, retrieval)
        return await composer.assemble(timeline)

    rendered = asyncio.run(_run())

    assert rendered.media_kind == MediaKind.VIDEO
    assert rendered.format == Format.SQUARE_1080
    assert rendered.duration_s is not None
    assert rendered.duration_s >= 0.5
    assert len(rendered.checksum) == 64


def test_assembles_with_ducked_music_and_voiceover(tmp_path: Path) -> None:
    async def _run() -> RenderedAsset:
        clip_id = AssetId.new()
        image_uri = StorageUri("image/a.png")
        voice_uri = StorageUri("audio/voice.mp3")
        music_uri = StorageUri("audio/music.mp3")
        assets = FakeCreativeAssetRepository(
            {
                clip_id: make_creative_asset(
                    asset_id=clip_id, storage_uri=image_uri, media_kind=MediaKind.IMAGE
                )
            }
        )
        retrieval = FakeAssetRetrieval(
            {
                image_uri.key: _tiny_png((10, 200, 10)),
                voice_uri.key: _tiny_silence_mp3(tmp_path, 0.3),
                music_uri.key: _tiny_silence_mp3(tmp_path, 0.3),
            }
        )
        timeline = Timeline(
            clips=(Clip(source=clip_id, start_s=0.0, end_s=0.3),),
            voiceover=AudioAsset(
                storage_uri=voice_uri,
                duration_s=0.3,
                renderer_used=RendererName.CHATTERBOX_ES_ES,
            ),
            music=AudioAsset(
                storage_uri=music_uri,
                duration_s=0.3,
                renderer_used=RendererName.ACE_STEP_1_5,
            ),
            subtitles=(),
            safe_area=SafeArea.none(),
            exports=(Format.STORY_1080X1920,),
        )
        composer = _composer(assets, retrieval)
        return await composer.assemble(timeline)

    rendered = asyncio.run(_run())

    assert rendered.format == Format.STORY_1080X1920
    assert rendered.duration_s is not None
