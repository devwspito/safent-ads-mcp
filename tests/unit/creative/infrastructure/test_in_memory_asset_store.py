"""`InMemoryAssetStore` (broker, lane 003): `put`+`pop` sin disco --
`render_image` los usa para nunca escribir en el directorio de activos de
`ads-api` (threat-model.md C-29, procesos distintos, sin disco
compartido)."""

from __future__ import annotations

import pytest

from safent_ads.creative.domain.enums import MediaKind
from safent_ads.creative.domain.storage import StorageUri
from safent_ads.creative.infrastructure.in_memory_asset_store import (
    InMemoryAssetStore,
    PreviewNotSupportedByBrokerStoreError,
    UnknownStorageUriError,
)

_PAYLOAD = b"\x89PNG\r\n\x1a\nfake-png-body"


async def test_put_then_pop_returns_the_same_bytes() -> None:
    store = InMemoryAssetStore()

    uri = await store.put(_PAYLOAD, MediaKind.IMAGE)

    assert store.pop(uri) == _PAYLOAD


async def test_put_returns_a_key_namespaced_by_media_kind() -> None:
    store = InMemoryAssetStore()

    uri = await store.put(_PAYLOAD, MediaKind.IMAGE)

    assert uri.key.startswith("image/")


async def test_pop_discards_the_payload_after_returning_it_once() -> None:
    store = InMemoryAssetStore()
    uri = await store.put(_PAYLOAD, MediaKind.IMAGE)

    store.pop(uri)

    with pytest.raises(UnknownStorageUriError):
        store.pop(uri)


def test_pop_an_unknown_key_raises() -> None:
    store = InMemoryAssetStore()

    with pytest.raises(UnknownStorageUriError):
        store.pop(StorageUri("image/never-stored"))


async def test_two_puts_never_collide_on_the_same_key() -> None:
    store = InMemoryAssetStore()

    first = await store.put(_PAYLOAD, MediaKind.IMAGE)
    second = await store.put(_PAYLOAD, MediaKind.IMAGE)

    assert first.key != second.key


async def test_signed_preview_url_is_not_supported() -> None:
    store = InMemoryAssetStore()
    uri = await store.put(_PAYLOAD, MediaKind.IMAGE)

    with pytest.raises(PreviewNotSupportedByBrokerStoreError):
        await store.signed_preview_url(uri, 60)


async def test_open_preview_is_not_supported() -> None:
    store = InMemoryAssetStore()

    with pytest.raises(PreviewNotSupportedByBrokerStoreError):
        await store.open_preview("image/anything", 0, "signature")
