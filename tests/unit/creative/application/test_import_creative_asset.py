"""`ImportCreativeAsset`: cierra el circulo de la delegacion a herramientas
nativas. Allow-list ANTES de cualquier fetch (fail closed); brief
inexistente tambien corta antes del fetch; feliz camino guarda el activo
con provenance `MODEL_GENERATED` y `renderer_used=HERMES_NATIVE_DELEGATED`."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime

import pytest

from safent_ads.creative.application.errors import CreativeBriefNotFoundError
from safent_ads.creative.application.import_creative_asset import (
    ImportCreativeAsset,
    ImportCreativeAssetRequest,
)
from safent_ads.creative.domain.asset_import import SourceUrlNotAllowedError
from safent_ads.creative.domain.enums import GenerationStatus, MediaKind, RendererName
from safent_ads.creative.domain.identifiers import BriefId
from safent_ads.creative.infrastructure.in_memory_repositories import (
    InMemoryCreativeAssetRepository,
    InMemoryCreativeBriefRepository,
)
from safent_ads.shared.clock import FixedClock
from tests.unit.creative.domain.factories import make_brief
from tests.unit.creative.infrastructure.fakes import FakeAssetStore

_ALLOWED_HOSTS = frozenset({"v3.fal.media"})


class _FakeAssetFetch:
    def __init__(self, payload: bytes = b"\x89PNG\r\n\x1a\nrest-of-file") -> None:
        self.payload = payload
        self.calls: list[str] = []

    async def fetch(self, url: str) -> bytes:
        self.calls.append(url)
        return self.payload


_UseCaseFixture = tuple[
    ImportCreativeAsset,
    InMemoryCreativeBriefRepository,
    InMemoryCreativeAssetRepository,
    _FakeAssetFetch,
]


def _build_use_case(*, allowed_hosts: frozenset[str] = _ALLOWED_HOSTS) -> _UseCaseFixture:
    briefs = InMemoryCreativeBriefRepository()
    assets = InMemoryCreativeAssetRepository()
    fetch = _FakeAssetFetch()
    use_case = ImportCreativeAsset(
        briefs=briefs,
        assets=assets,
        asset_fetch=fetch,
        asset_store=FakeAssetStore(),
        allowed_hosts=allowed_hosts,
        clock=FixedClock(datetime(2026, 1, 1, tzinfo=UTC)),
    )
    return use_case, briefs, assets, fetch


def test_imports_asset_with_delegated_provenance() -> None:
    async def _run() -> None:
        use_case, briefs, assets, fetch = _build_use_case()
        brief_id = BriefId.new()
        await briefs.add(brief_id, make_brief(variant_count=1))
        request = ImportCreativeAssetRequest(
            business_id=(await briefs.get(brief_id)).business_id,  # type: ignore[union-attr]
            brief_id=brief_id,
            source_signal_id=None,
            source_url="https://v3.fal.media/files/out.png",
            media_kind=MediaKind.IMAGE,
            native_tool_used="image_generate",
        )

        asset_id = await use_case.execute(request)

        asset = await assets.get(asset_id)
        assert asset is not None
        assert asset.provenance.renderer_used == RendererName.HERMES_NATIVE_DELEGATED
        assert asset.provenance.model_name == "image_generate"
        assert asset.provenance.generation_status == GenerationStatus.MODEL_GENERATED
        assert fetch.calls == ["https://v3.fal.media/files/out.png"]

    asyncio.run(_run())


def test_rejects_disallowed_host_before_fetching() -> None:
    async def _run() -> None:
        use_case, briefs, _, fetch = _build_use_case()
        brief_id = BriefId.new()
        await briefs.add(brief_id, make_brief(variant_count=1))
        request = ImportCreativeAssetRequest(
            business_id=(await briefs.get(brief_id)).business_id,  # type: ignore[union-attr]
            brief_id=brief_id,
            source_signal_id=None,
            source_url="https://evil.example/out.png",
            media_kind=MediaKind.IMAGE,
            native_tool_used="image_generate",
        )

        await use_case.execute(request)
        assert not fetch.calls

    with pytest.raises(SourceUrlNotAllowedError):
        asyncio.run(_run())


def test_raises_when_brief_does_not_exist() -> None:
    async def _run() -> None:
        use_case, _, _, _ = _build_use_case()
        request = ImportCreativeAssetRequest(
            business_id=(make_brief(variant_count=1)).business_id,
            brief_id=BriefId.new(),
            source_signal_id=None,
            source_url="https://v3.fal.media/files/out.png",
            media_kind=MediaKind.IMAGE,
            native_tool_used="image_generate",
        )

        await use_case.execute(request)

    with pytest.raises(CreativeBriefNotFoundError):
        asyncio.run(_run())


def test_checksum_is_sha256_of_the_fetched_payload() -> None:
    async def _run() -> str:
        use_case, briefs, assets, fetch = _build_use_case()
        brief_id = BriefId.new()
        await briefs.add(brief_id, make_brief(variant_count=1))
        request = ImportCreativeAssetRequest(
            business_id=(await briefs.get(brief_id)).business_id,  # type: ignore[union-attr]
            brief_id=brief_id,
            source_signal_id=None,
            source_url="https://v3.fal.media/files/out.png",
            media_kind=MediaKind.IMAGE,
            native_tool_used="image_generate",
        )

        asset_id = await use_case.execute(request)
        asset = await assets.get(asset_id)
        assert asset is not None
        assert asset.checksum == hashlib.sha256(fetch.payload).hexdigest()
        return asset.checksum

    asyncio.run(_run())
