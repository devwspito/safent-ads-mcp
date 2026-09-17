"""`HermesToolRenderer`: nunca completa un render, siempre construye una
`DelegationInstruction` generica (aspecto semantico, nunca un tamano en
pixeles fijo de un proveedor concreto) y la lanza en
`RendererDelegationRequiredError`."""

from __future__ import annotations

import asyncio

import pytest

from safent_ads.creative.domain.enums import Format, Language, RendererName
from safent_ads.creative.domain.render_specs import ImageSpec, VideoSpec, VoiceSpec
from safent_ads.creative.infrastructure.hermes_tool_renderer import (
    HermesToolRenderer,
    RendererDelegationRequiredError,
)
from tests.unit.creative.domain.factories import make_brand_kit


def test_name_is_the_delegated_renderer() -> None:
    renderer = HermesToolRenderer()

    assert renderer.name == RendererName.HERMES_NATIVE_DELEGATED


def test_image_render_raises_delegation_with_image_generate() -> None:
    async def _run() -> RendererDelegationRequiredError:
        renderer = HermesToolRenderer()
        spec = ImageSpec(
            prompt="estudiante concentrada estudiando",
            reference_assets=(),
            format=Format.STORY_1080X1920,
            seed=None,
            brand_kit=make_brand_kit(),
        )
        with pytest.raises(RendererDelegationRequiredError) as exc_info:
            await renderer.render(spec)
        return exc_info.value

    error = asyncio.run(_run())

    assert error.instruction.native_tool == "image_generate"
    assert error.instruction.arguments["aspect_ratio"] == "portrait"
    assert error.instruction.arguments["prompt"] == "estudiante concentrada estudiando"


def test_image_instruction_never_hardcodes_pixel_sizes() -> None:
    async def _run() -> RendererDelegationRequiredError:
        renderer = HermesToolRenderer()
        spec = ImageSpec(
            prompt="fondo de estudio",
            reference_assets=(),
            format=Format.SQUARE_1080,
            seed=None,
            brand_kit=make_brand_kit(),
        )
        with pytest.raises(RendererDelegationRequiredError) as exc_info:
            await renderer.render(spec)
        return exc_info.value

    error = asyncio.run(_run())

    assert error.instruction.arguments["aspect_ratio"] == "square"
    assert set(error.instruction.arguments) == {"prompt", "aspect_ratio"}


def test_video_render_raises_delegation_with_video_generate() -> None:
    async def _run() -> RendererDelegationRequiredError:
        renderer = HermesToolRenderer()
        spec = VideoSpec(
            key_frames=(),
            motion_prompt="camara lenta acercandose",
            duration_s=6,
            format=Format.STORY_1080X1920,
            seed=1,
        )
        with pytest.raises(RendererDelegationRequiredError) as exc_info:
            await renderer.render(spec)
        return exc_info.value

    error = asyncio.run(_run())

    assert error.instruction.native_tool == "video_generate"
    assert error.instruction.arguments["duration_s"] == 6
    assert error.instruction.arguments["aspect_ratio"] == "portrait"


def test_voice_synthesize_raises_delegation_with_text_to_speech() -> None:
    async def _run() -> RendererDelegationRequiredError:
        renderer = HermesToolRenderer()
        spec = VoiceSpec(text="Prepárate con Negocio Ejemplo", language=Language.ES_ES)
        with pytest.raises(RendererDelegationRequiredError) as exc_info:
            await renderer.synthesize(spec)
        return exc_info.value

    error = asyncio.run(_run())

    assert error.instruction.native_tool == "text_to_speech"
    assert error.instruction.arguments["language"] == "es-ES"


def test_instruction_tells_the_agent_to_close_the_loop_with_import() -> None:
    async def _run() -> RendererDelegationRequiredError:
        renderer = HermesToolRenderer()
        spec = ImageSpec(
            prompt="fondo",
            reference_assets=(),
            format=Format.SQUARE_1080,
            seed=None,
            brand_kit=make_brand_kit(),
        )
        with pytest.raises(RendererDelegationRequiredError) as exc_info:
            await renderer.render(spec)
        return exc_info.value

    error = asyncio.run(_run())

    assert "import_creative_asset" in error.instruction.next_step
