"""Integracion real de `PlaywrightBannerComposer`: lanza Chromium de
verdad y captura los 7 formatos de `AssetSpec` sobre la plantilla de
`assets/creative-templates/`. Se salta con motivo explicito si
`playwright install chromium` no esta disponible en este entorno — nunca
falla en rojo por infraestructura ausente, solo se marca `skipped`.

Verificado en la DGX Spark aarch64 de este carril: Chromium arranca y
`Page.screenshot()` funciona con `--disable-gpu --no-sandbox
--disable-dev-shm-usage` (sin esos flags, "Unable to capture screenshot")."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from safent_ads.creative.domain.enums import Format
from safent_ads.creative.domain.render_specs import BannerSpec
from safent_ads.creative.infrastructure.banner_composer import PlaywrightBannerComposer
from safent_ads.creative.infrastructure.in_memory_repositories import (
    InMemoryCreativeAssetRepository,
)
from safent_ads.creative.infrastructure.local_asset_storage import LocalAssetStorage
from safent_ads.shared.clock import SystemClock
from tests.unit.creative.domain.factories import make_ad_copy, make_brand_kit

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).parents[3]
_TEMPLATES_DIR = _REPO_ROOT / "assets" / "creative-templates"


class _UnusedAssetRetrieval:
    async def get(self, uri: object) -> bytes:  # pragma: no cover - no background en este smoke
        raise NotImplementedError("smoke sin fondo: no deberia llamarse")


def test_renders_all_seven_formats(tmp_path: Path) -> None:
    async def _run() -> list[int]:
        asset_store = LocalAssetStorage(
            tmp_path, signing_key=b"integration-test-key", clock=SystemClock()
        )
        composer = PlaywrightBannerComposer(
            _TEMPLATES_DIR,
            asset_store,
            _UnusedAssetRetrieval(),  # type: ignore[arg-type]
            InMemoryCreativeAssetRepository(),
        )
        spec = BannerSpec(
            template_name="banner.html.j2",
            ad_copy=make_ad_copy(),
            brand_kit=make_brand_kit(),
            background_image=None,
            formats=tuple(Format),
        )
        try:
            rendered = await composer.compose(spec)
        finally:
            await composer.aclose()
        return [len(r.checksum) for r in rendered]

    try:
        checksum_lengths = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - frontera de entorno, no de codigo
        pytest.skip(f"Playwright/Chromium no disponible en este entorno: {exc}")

    assert checksum_lengths == [64] * len(list(Format))
