"""`build_image_renderers` (composition/creative_renderers.py): cierre en
falso sin clave -- `dict` vacio, exactamente como `build_registry
(local_enabled=False)` deja los descriptores `LOCAL_GPU` ausentes -- y con
clave, el adaptador correcto queda registrado bajo su propio `RendererName`.
`ADS_FAL_IMAGE_MODEL`/`ADS_FAL_IMAGE_PRICE_USD` (composition/settings.py,
`BrokerSettings`, lane creative): valores por defecto que mantienen
paridad con Hermes."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from safent_ads.composition.creative_renderers import build_image_renderers
from safent_ads.composition.settings import BrokerSettings
from safent_ads.creative.domain.enums import RendererName
from safent_ads.creative.infrastructure.fal_image_adapter import FalImageRenderer
from safent_ads.creative.infrastructure.openai_image_adapter import OpenAiImageRenderer
from safent_ads.shared.clock import SystemClock
from tests.unit.creative.infrastructure.fakes import FakeAssetStore


def _broker_settings(**overrides: Any) -> BrokerSettings:
    defaults: dict[str, Any] = {
        "broker_socket_path": "/tmp/safent-ads-test/broker.sock",
        "approval_public_key": "test-public-key",
        "allowed_uids": [10001],
        "hard_caps_file": "/tmp/safent-ads-test/caps.yaml",
        "credential_master_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "credential_store_dir": "/tmp/safent-ads-test/credentials",
        # Explicito, no confiar en que el entorno real este limpio: pydantic-
        # settings lee variables de entorno de verdad pese a `_env_file=None`
        # (solo desactiva el fichero dotenv), y un `OPENAI_API_KEY`/
        # `FAL_API_KEY` de la sesion del propietario haria que
        # `test_build_image_renderers_is_empty_without_any_key` dependiera
        # del shell en el que corre, no del codigo.
        "openai_api_key": None,
        "fal_api_key": None,
    }
    defaults.update(overrides)
    return BrokerSettings(_env_file=None, **defaults)


def test_fal_image_model_defaults_to_the_hermes_parity_model() -> None:
    settings = _broker_settings()

    assert settings.fal_image_model == "fal-ai/flux-2/klein/9b"


def test_fal_image_price_defaults_to_the_documented_estimate() -> None:
    settings = _broker_settings()

    assert settings.fal_image_price_usd == Decimal("0.02")


def test_fal_image_model_and_price_are_configurable() -> None:
    settings = _broker_settings(
        fal_image_model="fal-ai/flux-2/klein/4b",
        fal_image_price_usd=Decimal("0.05"),
    )

    assert settings.fal_image_model == "fal-ai/flux-2/klein/4b"
    assert settings.fal_image_price_usd == Decimal("0.05")


def test_build_image_renderers_is_empty_without_any_key() -> None:
    settings = _broker_settings()

    renderers = build_image_renderers(settings, asset_store=FakeAssetStore(), clock=SystemClock())

    assert renderers == {}


def test_build_image_renderers_registers_fal_when_its_key_is_set() -> None:
    settings = _broker_settings(fal_api_key="fal-test-key")

    renderers = build_image_renderers(settings, asset_store=FakeAssetStore(), clock=SystemClock())

    assert set(renderers) == {RendererName.FLUX2_KLEIN_9B}
    assert isinstance(renderers[RendererName.FLUX2_KLEIN_9B], FalImageRenderer)


def test_build_image_renderers_registers_openai_when_its_key_is_set() -> None:
    settings = _broker_settings(openai_api_key="sk-test")

    renderers = build_image_renderers(settings, asset_store=FakeAssetStore(), clock=SystemClock())

    assert set(renderers) == {RendererName.GPT_IMAGE_1_5}
    assert isinstance(renderers[RendererName.GPT_IMAGE_1_5], OpenAiImageRenderer)


def test_build_image_renderers_registers_both_when_both_keys_are_set() -> None:
    settings = _broker_settings(fal_api_key="fal-test-key", openai_api_key="sk-test")

    renderers = build_image_renderers(settings, asset_store=FakeAssetStore(), clock=SystemClock())

    assert set(renderers) == {RendererName.FLUX2_KLEIN_9B, RendererName.GPT_IMAGE_1_5}


def test_build_image_renderers_uses_the_configured_fal_model_and_price() -> None:
    settings = _broker_settings(
        fal_api_key="fal-test-key",
        fal_image_model="fal-ai/flux-2/klein/4b",
        fal_image_price_usd=Decimal("0.07"),
    )

    renderers = build_image_renderers(settings, asset_store=FakeAssetStore(), clock=SystemClock())

    fal_renderer = renderers[RendererName.FLUX2_KLEIN_9B]
    assert isinstance(fal_renderer, FalImageRenderer)
    assert fal_renderer._model_path == "fal-ai/flux-2/klein/4b"  # noqa: SLF001
    assert fal_renderer._price_per_image == Decimal("0.07")  # noqa: SLF001
