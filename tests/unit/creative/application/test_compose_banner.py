"""`ComposeBanner`: la garantia de entrega (tool-surface.md §2.2
`compose_banner_set` — "los 7 tamaños con el texto exacto compuesto en
HTML"). Sin `background_image` produce igual los 7 tamaños, incluidos
728x90 y 320x50, que ningun renderizador puede generar jamas
(`Format.is_renderer_eligible`): la composicion es CPU-only y no depende
de que ningun modelo generativo, delegado o directo, haya funcionado."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime

from safent_ads.creative.application.compose_banner import ComposeBanner, ComposeBannerRequest
from safent_ads.creative.domain.enums import Format, GenerationStatus, MediaKind, RendererName
from safent_ads.creative.domain.identifiers import BriefId
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.render_specs import BannerSpec, RenderedAsset
from safent_ads.creative.domain.storage import StorageUri
from safent_ads.shared.ids import BusinessId
from tests.unit.creative.domain.factories import make_ad_copy, make_brand_kit
from tests.unit.creative.infrastructure.fakes import FakeCreativeAssetRepository

_ALL_SEVEN_FORMATS: tuple[Format, ...] = tuple(Format)


class _FakeBannerComposer:
    """Simula `PlaywrightBannerComposer`: un `RenderedAsset` BANNER por
    `Format` pedido, con o sin fondo — el compositor nunca falla porque no
    depende de ningun renderizador generativo."""

    def __init__(self) -> None:
        self.calls: list[BannerSpec] = []

    async def compose(self, spec: BannerSpec) -> Sequence[RenderedAsset]:
        self.calls.append(spec)
        return [
            RenderedAsset(
                storage_uri=StorageUri(f"banner/{fmt.value}.png"),
                media_kind=MediaKind.BANNER,
                format=fmt,
                checksum=hashlib.sha256(fmt.value.encode()).hexdigest(),
                renderer_used=RendererName.HTML_BANNER_COMPOSER,
                cost_estimate=Money.zero("USD"),
                duration_s=None,
                generated_at=datetime.now(UTC),
            )
            for fmt in spec.formats
        ]


def _request(*, background_asset_id: object = None) -> ComposeBannerRequest:
    return ComposeBannerRequest(
        business_id=BusinessId.new(),
        brief_id=BriefId.new(),
        source_signal_id=None,
        template_name="ejemplo-default.html",
        ad_copy=make_ad_copy(),
        brand_kit=make_brand_kit(),
        background_asset_id=background_asset_id,  # type: ignore[arg-type]
        destination_url="https://ejemplo.es/landing",
        formats=_ALL_SEVEN_FORMATS,
    )


def test_produces_all_seven_ad_sizes_with_no_background_at_all() -> None:
    """La garantia de entrega: aunque NINGUN renderizador haya producido
    jamas un visual (delegacion pendiente, sin clave de proveedor, local
    apagado), el brief nunca sale con las manos vacias."""

    async def _run() -> list[Format]:
        assets = FakeCreativeAssetRepository()
        composer = _FakeBannerComposer()
        use_case = ComposeBanner(composer, assets)

        created_ids = await use_case.execute(_request(background_asset_id=None))

        created = [await assets.get(asset_id) for asset_id in created_ids]
        return [asset.format for asset in created if asset is not None and asset.format]

    formats = asyncio.run(_run())
    assert set(formats) == set(_ALL_SEVEN_FORMATS)
    assert len(formats) == len(_ALL_SEVEN_FORMATS)


def test_includes_the_two_formats_no_model_can_ever_generate() -> None:
    async def _run() -> set[Format]:
        assets = FakeCreativeAssetRepository()
        use_case = ComposeBanner(_FakeBannerComposer(), assets)

        created_ids = await use_case.execute(_request(background_asset_id=None))

        created = [await assets.get(asset_id) for asset_id in created_ids]
        return {asset.format for asset in created if asset is not None and asset.format}

    formats = asyncio.run(_run())
    assert Format.LEADERBOARD_728X90 in formats
    assert Format.MOBILE_LEADERBOARD_320X50 in formats
    assert not Format.LEADERBOARD_728X90.is_renderer_eligible
    assert not Format.MOBILE_LEADERBOARD_320X50.is_renderer_eligible


def test_marks_provenance_as_composer_only_fallback_without_background() -> None:
    async def _run() -> GenerationStatus:
        assets = FakeCreativeAssetRepository()
        use_case = ComposeBanner(_FakeBannerComposer(), assets)

        created_ids = await use_case.execute(_request(background_asset_id=None))

        asset = await assets.get(created_ids[0])
        assert asset is not None
        return asset.provenance.generation_status

    assert asyncio.run(_run()) == GenerationStatus.COMPOSER_ONLY_FALLBACK
