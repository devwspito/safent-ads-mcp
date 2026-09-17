"""T035 finding 1 (threat-model.md #17):
`GoogleAdsAdapter.upload_asset` via `AssetService.mutate_assets` (SDK v25)
with an `ImageAsset`. Same shape as `google_reference_reader.py`: a narrow
`Protocol` for the SDK slice the broker needs, wired in production by a
thin wrapper over the real SDK (composition, another lane), plus pure/async
orchestration the adapter calls -- no Google SDK type ever appears here.

Google Ads does not deduplicate image assets by content the way Meta's
`adimages` does (Meta computes the `hash` from the bytes for free) -- this
module derives a checksum from the request bytes and uses it as the new
asset's `name`, so a GAQL lookup on that exact name (the adapter's own
`run_gaql` path) resolves the SAME `resource_name` on a retry, instead of
minting a duplicate asset (BL-6 R2.7, deliverable 2)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from typing import Final, Protocol

from safent_ads.accounts.application.ports import AssetUploadRequest, PlatformAssetHandle

__all__ = [
    "ALLOWED_IMAGE_MIME_TYPES",
    "MAX_IMAGE_UPLOAD_BYTES",
    "GoogleAssetUploadClient",
    "asset_checksum",
    "upload_image_asset",
]

# Mirrors `MetaAdsAdapter._ALLOWED_CREATIVE_MIME_TYPES`/
# `_MAX_CREATIVE_UPLOAD_BYTES` (deliverable 2): the two platforms share the
# same creative pipeline upstream (`creative/domain/enums.py`), so there is
# no reason for Google to admit a type or size Meta would reject.
ALLOWED_IMAGE_MIME_TYPES: Final = frozenset({"image/jpeg", "image/png"})
MAX_IMAGE_UPLOAD_BYTES: Final = 8 * 1024 * 1024


class GoogleAssetUploadClient(Protocol):
    """Subset of `AssetService` (v25) the broker needs: a single
    `mutate_assets` call with one `ImageAsset` (`data`, `mime_type`, a
    `name` derived from the checksum, `full_size` width/height). In
    production, a thin wrapper over the SDK; in tests, a plain double."""

    def mutate_image_asset(
        self,
        customer_id: str,
        *,
        name: str,
        data: bytes,
        mime_type: str,
        width: int,
        height: int,
    ) -> str: ...


def asset_checksum(media: bytes) -> str:
    return hashlib.sha256(media).hexdigest()


async def upload_image_asset(
    client: GoogleAssetUploadClient,
    request: AssetUploadRequest,
    *,
    customer_id: str,
    checksum: str,
    find_existing: Callable[[str], Awaitable[str | None]],
) -> PlatformAssetHandle:
    """Caller (`GoogleAdsAdapter.upload_asset`) already validated
    mime/size/width/height -- this only resolves idempotency and mutates.
    `find_existing` is the adapter's own GAQL lookup (`asset.name =
    checksum`); a hit means the SAME bytes were uploaded before, and
    `AssetService.mutate_assets` is never called a second time for them."""
    existing = await find_existing(checksum)
    if existing is not None:
        return PlatformAssetHandle(platform_asset_id=existing, preview_url=None)
    width = request.width
    height = request.height
    assert width is not None and height is not None  # noqa: S101 - validated by the caller
    resource_name = await asyncio.to_thread(
        client.mutate_image_asset,
        customer_id,
        name=checksum,
        data=request.media,
        mime_type=request.mime_type,
        width=width,
        height=height,
    )
    return PlatformAssetHandle(platform_asset_id=resource_name, preview_url=None)
