"""Dobles de puertos reutilizados por los tests de `infrastructure/`. No son
mocks de libreria: implementan el `Protocol` real para que un cambio de
firma en `application/ports.py` rompa el test, no lo silencie."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from safent_ads.creative.domain.copy import AdCopy
from safent_ads.creative.domain.creative_asset import CreativeAsset, Provenance
from safent_ads.creative.domain.enums import Format, GenerationStatus, MediaKind, RendererName
from safent_ads.creative.domain.identifiers import AssetId, BriefId, SignalId
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.storage import StorageUri
from safent_ads.shared.ids import BusinessId


def make_creative_asset(
    *,
    asset_id: AssetId | None = None,
    business_id: BusinessId | None = None,
    storage_uri: StorageUri | None = None,
    media_kind: MediaKind = MediaKind.IMAGE,
    ad_copy: AdCopy | None = None,
    destination_url: str | None = None,
    generated_at: datetime | None = None,
    brief_id: BriefId | None = None,
) -> CreativeAsset:
    return CreativeAsset(
        asset_id=asset_id or AssetId.new(),
        business_id=business_id or BusinessId.new(),
        media_kind=media_kind,
        format=Format.SQUARE_1080,
        duration_seconds=None,
        storage_uri=storage_uri or StorageUri("image/x.png"),
        checksum="a" * 64,
        cost_estimate=Money(Decimal("0"), "USD"),
        provenance=Provenance(
            renderer_used=RendererName.QWEN_IMAGE_2512,
            model_name="qwen-image-2512-lightning-4step",
            seed=1,
            brief_id=brief_id or BriefId.new(),
            source_signal_id=SignalId(uuid.uuid4()),
            generation_status=GenerationStatus.MODEL_GENERATED,
            generated_at=generated_at or datetime(2026, 1, 1, tzinfo=UTC),
        ),
        ad_copy=ad_copy,
        destination_url=destination_url,
    )


class FakeAssetStore:
    def __init__(self) -> None:
        self.puts: list[tuple[bytes, MediaKind]] = []
        self._payload_by_key: dict[str, bytes] = {}

    async def put(self, payload: bytes, media_kind: MediaKind) -> StorageUri:
        self.puts.append((payload, media_kind))
        key = f"{media_kind.value}/fake-{len(self.puts)}.bin"
        self._payload_by_key[key] = payload
        return StorageUri(key)

    async def signed_preview_url(self, uri: StorageUri, ttl_s: int) -> str:
        return f"/api/v1/creative-previews/{uri.key}?exp=9999999999&sig=fake&ttl={ttl_s}"

    async def open_preview(self, key: str, expires_at: int, signature: str) -> bytes:
        # Doble de prueba del router: `test_local_asset_storage.py` cubre
        # firma/caducidad/traversal de verdad contra `LocalAssetStorage`;
        # este solo necesita devolver los bytes que `put` ya guardo.
        del expires_at, signature
        return self._payload_by_key[key]


class FakeAssetRetrieval:
    def __init__(self, payload_by_key: dict[str, bytes]) -> None:
        self._payload_by_key = payload_by_key

    async def get(self, uri: StorageUri) -> bytes:
        return self._payload_by_key[uri.key]


class FakeCreativeAssetRepository:
    def __init__(self, assets: dict[AssetId, CreativeAsset] | None = None) -> None:
        self._assets = dict(assets or {})

    async def get(self, asset_id: AssetId) -> CreativeAsset | None:
        return self._assets.get(asset_id)

    async def add(self, asset: CreativeAsset) -> None:
        self._assets[asset.asset_id] = asset

    async def update(self, asset: CreativeAsset) -> None:
        self._assets[asset.asset_id] = asset

    async def list_for_business(
        self, business_id: BusinessId, *, media_kind: MediaKind | None = None
    ) -> tuple[CreativeAsset, ...]:
        return tuple(
            asset
            for asset in self._assets.values()
            if asset.business_id == business_id
            and (media_kind is None or asset.media_kind == media_kind)
        )
