"""`RendererSelector`: delegado primero, proveedor directo despues, local al
final y solo si `local_enabled=True` (creative-port.md §"Seleccion de
renderizador", invertido 2026-09-09, T099
`test_non_commercial_renderer_absent`)."""

from __future__ import annotations

import pytest

from safent_ads.creative.domain.enums import RendererIntent, RendererName, RendererTier
from safent_ads.creative.domain.renderer_selector import (
    MODEL_NAME_BY_RENDERER,
    NoRendererAvailableError,
    RendererDescriptor,
    RendererSelector,
    build_registry,
)

_NON_COMMERCIAL_NAMES = {"narratoai", "f5_tts", "fish", "xtts", "musicgen", "flux2_dev", "sd35"}


def test_non_commercial_renderer_absent_from_enum() -> None:
    registered_values = {name.value for name in RendererName}

    assert registered_values.isdisjoint(_NON_COMMERCIAL_NAMES)


def test_descriptor_rejects_non_commercial_use() -> None:
    with pytest.raises(ValueError, match="uso comercial"):
        RendererDescriptor(
            name=RendererName.QWEN_IMAGE_2512,
            commercial_use=False,
            tier=RendererTier.LOCAL_GPU,
            intents=frozenset({RendererIntent.IMAGE_WITH_TEXT}),
        )


def test_select_prefers_delegated_renderer() -> None:
    selector = RendererSelector()

    chosen = selector.select(RendererIntent.IMAGE_WITH_TEXT)

    assert chosen == RendererName.HERMES_NATIVE_DELEGATED


def test_candidates_for_orders_delegated_before_direct_provider() -> None:
    selector = RendererSelector()

    candidates = selector.candidates_for(RendererIntent.IMAGE_WITH_TEXT)

    assert candidates == (
        RendererName.HERMES_NATIVE_DELEGATED,
        RendererName.FLUX2_KLEIN_9B,
        RendererName.GPT_IMAGE_1_5,
    )


def test_flux2_klein_9b_fal_precedes_gpt_image_among_direct_providers() -> None:
    # Encargo del propietario 2026-09-15 ("total parity"): fal.ai
    # flux-2/klein/9b es el mismo proveedor+modelo que Hermes ya usa por
    # defecto, asi que es la primera preferencia de proveedor directo;
    # OpenAI sigue disponible como alternativa.
    selector = RendererSelector()

    candidates = selector.candidates_for(RendererIntent.IMAGE_WITH_TEXT)

    assert candidates.index(RendererName.FLUX2_KLEIN_9B) < candidates.index(
        RendererName.GPT_IMAGE_1_5
    )


def test_candidates_for_never_includes_local_gpu_by_default() -> None:
    selector = RendererSelector()

    candidates = selector.candidates_for(RendererIntent.IMAGE_WITH_TEXT)

    assert RendererName.QWEN_IMAGE_2512 not in candidates


def test_build_registry_with_local_enabled_appends_local_gpu_last() -> None:
    selector = RendererSelector(build_registry(local_enabled=True))

    candidates = selector.candidates_for(RendererIntent.IMAGE_WITH_TEXT)

    assert candidates == (
        RendererName.HERMES_NATIVE_DELEGATED,
        RendererName.FLUX2_KLEIN_9B,
        RendererName.GPT_IMAGE_1_5,
        RendererName.QWEN_IMAGE_2512,
    )


def test_select_raises_when_no_renderer_registered_for_intent() -> None:
    empty_selector = RendererSelector(registry=())

    with pytest.raises(NoRendererAvailableError):
        empty_selector.select(RendererIntent.MUSIC)


def test_video_vertical_fast_prefers_ken_burns_before_generative() -> None:
    selector = RendererSelector()

    candidates = selector.candidates_for(RendererIntent.VIDEO_VERTICAL_FAST)

    assert candidates[0] == RendererName.KEN_BURNS_VIDEO_COMPOSER


def test_is_registered() -> None:
    selector = RendererSelector(build_registry(local_enabled=True))

    assert selector.is_registered(RendererName.LTX_2_5)


def test_is_registered_false_for_local_gpu_when_disabled() -> None:
    selector = RendererSelector()

    assert not selector.is_registered(RendererName.LTX_2_5)


def test_every_renderer_name_has_a_model_name_for_provenance() -> None:
    for name in RendererName:
        assert MODEL_NAME_BY_RENDERER[name]


def test_qwen_model_name_matches_the_verified_dgx_checkpoint() -> None:
    assert MODEL_NAME_BY_RENDERER[RendererName.QWEN_IMAGE_2512] == "qwen-image-2512-lightning-4step"


def test_flux2_klein_9b_is_registered_by_default() -> None:
    selector = RendererSelector()

    assert selector.is_registered(RendererName.FLUX2_KLEIN_9B)


def test_flux2_klein_9b_model_name_is_distinct_from_the_local_gpu_checkpoint() -> None:
    assert MODEL_NAME_BY_RENDERER[RendererName.FLUX2_KLEIN_9B] == "flux-2-klein-9b"
    assert (
        MODEL_NAME_BY_RENDERER[RendererName.FLUX2_KLEIN_9B]
        != MODEL_NAME_BY_RENDERER[RendererName.FLUX2_KLEIN]
    )
