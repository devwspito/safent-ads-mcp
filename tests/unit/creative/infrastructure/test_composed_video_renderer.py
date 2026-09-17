"""`ComposedVideoRenderer`: Ken Burns sobre un keyvisual sintetico diminuto,
ffmpeg real, sin red (mismo criterio que `test_video_composer.py`, T105).
Siempre disponible: no depende de ninguna clave de proveedor."""

from __future__ import annotations

import asyncio
import io
import shutil

import pytest
from PIL import Image

from safent_ads.creative.application.errors import CreativeAssetNotFoundError
from safent_ads.creative.domain.enums import Format, MediaKind, RendererName
from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.creative.domain.render_specs import VideoSpec
from safent_ads.creative.domain.storage import StorageUri
from safent_ads.creative.infrastructure.video_composer import (
    ComposedVideoRenderer,
    ComposedVideoRendererError,
)
from tests.unit.creative.infrastructure.fakes import (
    FakeAssetRetrieval,
    FakeAssetStore,
    FakeCreativeAssetRepository,
    make_creative_asset,
)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg no disponible")


def _tiny_png(width: int = 96, height: int = 96) -> bytes:
    image = Image.new("RGB", (width, height), color=(30, 60, 90))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_renders_a_video_from_a_single_still() -> None:
    async def _run() -> tuple[MediaKind, Format, RendererName]:
        key_frame_id = AssetId.new()
        key_frame_uri = StorageUri("image/keyframe.png")
        assets = FakeCreativeAssetRepository(
            {key_frame_id: make_creative_asset(asset_id=key_frame_id, storage_uri=key_frame_uri)}
        )
        asset_retrieval = FakeAssetRetrieval({key_frame_uri.key: _tiny_png()})
        asset_store = FakeAssetStore()
        renderer = ComposedVideoRenderer(assets, asset_retrieval, asset_store)
        spec = VideoSpec(
            key_frames=(key_frame_id,),
            motion_prompt="zoom lento sobre el aula",
            duration_s=1,
            format=Format.SQUARE_1080,
            seed=1,
        )

        result = await renderer.render(spec)

        return result.media_kind, result.format, result.renderer_used

    media_kind, fmt, renderer_used = asyncio.run(_run())
    assert media_kind == MediaKind.VIDEO
    assert fmt == Format.SQUARE_1080
    assert renderer_used == RendererName.KEN_BURNS_VIDEO_COMPOSER


def test_stored_payload_looks_like_an_mp4_container() -> None:
    async def _run() -> bytes:
        key_frame_id = AssetId.new()
        key_frame_uri = StorageUri("image/keyframe.png")
        assets = FakeCreativeAssetRepository(
            {key_frame_id: make_creative_asset(asset_id=key_frame_id, storage_uri=key_frame_uri)}
        )
        asset_retrieval = FakeAssetRetrieval({key_frame_uri.key: _tiny_png()})
        asset_store = FakeAssetStore()
        renderer = ComposedVideoRenderer(assets, asset_retrieval, asset_store)
        spec = VideoSpec(
            key_frames=(key_frame_id,),
            motion_prompt="zoom lento",
            duration_s=1,
            format=Format.STORY_1080X1920,
            seed=1,
        )

        await renderer.render(spec)
        return asset_store.puts[0][0]

    payload = asyncio.run(_run())
    assert payload[4:8] == b"ftyp"


def test_raises_when_no_key_frame_given() -> None:
    async def _run() -> None:
        renderer = ComposedVideoRenderer(
            FakeCreativeAssetRepository(), FakeAssetRetrieval({}), FakeAssetStore()
        )
        spec = VideoSpec(
            key_frames=(),
            motion_prompt="zoom lento",
            duration_s=1,
            format=Format.SQUARE_1080,
            seed=1,
        )
        await renderer.render(spec)

    with pytest.raises(ComposedVideoRendererError):
        asyncio.run(_run())


def test_raises_when_key_frame_asset_missing() -> None:
    async def _run() -> None:
        renderer = ComposedVideoRenderer(
            FakeCreativeAssetRepository(), FakeAssetRetrieval({}), FakeAssetStore()
        )
        spec = VideoSpec(
            key_frames=(AssetId.new(),),
            motion_prompt="zoom lento",
            duration_s=1,
            format=Format.SQUARE_1080,
            seed=1,
        )
        await renderer.render(spec)

    with pytest.raises(CreativeAssetNotFoundError):
        asyncio.run(_run())
