"""`ChatterboxVoiceRenderer`, `AceStepMusicRenderer`, `FalVideoRenderer`:
adaptadores HTTP contra servidores falsos (`httpx.MockTransport`), sin red
real (T103). `OpenAiImageRenderer` tiene su propio fichero
(`test_openai_image_adapter.py`): la logica de recorte/coste por tokens
merece sus propios casos, no compartir este archivo por inercia."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from safent_ads.creative.domain.enums import Format, Language, MediaKind, RendererName
from safent_ads.creative.domain.render_specs import MusicSpec, VideoSpec, VoiceSpec
from safent_ads.creative.infrastructure.acestep_music_adapter import (
    AceStepMusicRenderer,
    AceStepNotConfiguredError,
)
from safent_ads.creative.infrastructure.chatterbox_tts_adapter import ChatterboxVoiceRenderer
from safent_ads.creative.infrastructure.fal_adapter import FalTimeoutError, FalVideoRenderer
from tests.unit.creative.infrastructure.fakes import FakeAssetStore


def test_chatterbox_synthesize_returns_audio_asset() -> None:
    async def _run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/tts"
            return httpx.Response(200, content=b"RIFF-fake-wav-bytes")

        asset_store = FakeAssetStore()
        renderer = ChatterboxVoiceRenderer(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            base_url="http://127.0.0.1:8004",
            asset_store=asset_store,
        )
        spec = VoiceSpec(text="Prepárate con Negocio Ejemplo", language=Language.ES_ES)
        result = await renderer.synthesize(spec)

        assert result.renderer_used == RendererName.CHATTERBOX_ES_ES
        assert result.duration_s > 0
        assert asset_store.puts[0][1] == MediaKind.AUDIO

    asyncio.run(_run())


def test_acestep_without_base_url_raises_not_configured() -> None:
    async def _run() -> None:
        renderer = AceStepMusicRenderer(
            httpx.AsyncClient(), base_url=None, asset_store=FakeAssetStore()
        )
        await renderer.compose(MusicSpec(mood_prompt="animado", duration_s=60))

    with pytest.raises(AceStepNotConfiguredError):
        asyncio.run(_run())


def test_acestep_with_base_url_calls_generate() -> None:
    async def _run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/generate"
            return httpx.Response(200, content=b"fake-music-bytes")

        renderer = AceStepMusicRenderer(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            base_url="http://127.0.0.1:8001",
            asset_store=FakeAssetStore(),
        )
        result = await renderer.compose(MusicSpec(mood_prompt="animado", duration_s=60))

        assert result.renderer_used == RendererName.ACE_STEP_1_5
        assert result.duration_s == 60.0

    asyncio.run(_run())


def test_fal_video_renderer_polls_until_completed() -> None:
    async def _run() -> None:
        call_count = {"status": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/wan-2.5/image-to-video"):
                return httpx.Response(200, json={"request_id": "req-1"})
            if request.url.path.endswith("/status"):
                call_count["status"] += 1
                status = "COMPLETED" if call_count["status"] >= 2 else "IN_PROGRESS"
                return httpx.Response(200, json={"status": status})
            if request.url.path.endswith("/requests/req-1"):
                return httpx.Response(200, json={"video": {"url": "https://fal.media/out.mp4"}})
            if request.url.path == "/out.mp4":
                return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42")
            raise AssertionError(f"unexpected path {request.url.path}")

        asset_store = FakeAssetStore()
        renderer = FalVideoRenderer(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            api_key="fal-test-key",
            asset_store=asset_store,
            poll_interval_s=0.01,
        )
        spec = VideoSpec(
            key_frames=(),
            motion_prompt="camara lenta",
            duration_s=5,
            format=Format.STORY_1080X1920,
            seed=1,
        )
        result = await renderer.render(spec)

        assert result.renderer_used == RendererName.WAN_2_2
        assert result.duration_s == 5.0

    asyncio.run(_run())


def test_fal_video_renderer_times_out() -> None:
    async def _run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/wan-2.5/image-to-video"):
                return httpx.Response(200, json={"request_id": "req-1"})
            return httpx.Response(200, json={"status": "IN_PROGRESS"})

        renderer = FalVideoRenderer(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            api_key="fal-test-key",
            asset_store=FakeAssetStore(),
            timeout_s=0.05,
            poll_interval_s=0.01,
        )
        spec = VideoSpec(
            key_frames=(),
            motion_prompt="camara lenta",
            duration_s=5,
            format=Format.STORY_1080X1920,
            seed=1,
        )
        await renderer.render(spec)

    with pytest.raises(FalTimeoutError):
        asyncio.run(_run())
