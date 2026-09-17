"""Doble en memoria de `BrandAssetStoragePort` para tests de `application`."""

from __future__ import annotations

from collections.abc import Mapping

from safent_ads.brand.domain.brand_asset import AssetKind


class InMemoryBrandAssetStorage:
    def __init__(self, seed: Mapping[str, bytes] | None = None) -> None:
        self.stored: dict[str, tuple[bytes, AssetKind]] = {}
        self._payload_by_key: dict[str, bytes] = dict(seed or {})
        self._next_key = 0

    async def put(self, payload: bytes, kind: AssetKind) -> str:
        self._next_key += 1
        key = f"{kind.value}/fake-{self._next_key}"
        self.stored[key] = (payload, kind)
        self._payload_by_key[key] = payload
        return key

    async def get(self, key: str) -> bytes:
        return self._payload_by_key[key]
