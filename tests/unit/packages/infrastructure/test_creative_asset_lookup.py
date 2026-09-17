"""`PackageCreativeAssetLookup` (ME-7): mismo `details.reason` -- aqui,
`None` -- para activo inexistente, ajeno, no `READY` o sin veredicto
`PASS`; nunca distingue el motivo (contracts/mcp-tools.md Revision 2
§R2.3)."""

from __future__ import annotations

from safent_ads.creative.domain.enums import MediaKind, PolicyVerdictResult
from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.creative.domain.policy import PolicyVerdict
from safent_ads.packages.infrastructure.creative_asset_lookup import PackageCreativeAssetLookup
from safent_ads.shared.ids import BusinessId
from tests.unit.creative.infrastructure.fakes import (
    FakeAssetRetrieval,
    FakeCreativeAssetRepository,
    make_creative_asset,
)

_PNG_MAGIC_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 16


def _lookup(asset, *, payload: bytes = _PNG_MAGIC_BYTES) -> PackageCreativeAssetLookup:
    repo = FakeCreativeAssetRepository({asset.asset_id: asset} if asset else {})
    retrieval = FakeAssetRetrieval({str(asset.storage_uri): payload} if asset else {})
    return PackageCreativeAssetLookup(repo, retrieval)


async def test_ready_image_with_pass_verdict_is_usable() -> None:
    business_id = BusinessId.new()
    asset = make_creative_asset(business_id=business_id)
    asset.mark_ready(PolicyVerdict(verdict=PolicyVerdictResult.PASS_, findings=()))
    lookup = _lookup(asset)

    snapshot = await lookup.find_usable(business_id=business_id, asset_id=str(asset.asset_id))

    assert snapshot is not None
    assert snapshot.asset_id == str(asset.asset_id)
    assert snapshot.checksum == asset.checksum
    assert snapshot.mime_type == "image/png"
    assert snapshot.width == asset.format.width  # type: ignore[union-attr]
    assert snapshot.preview_key == str(asset.storage_uri)


async def test_get_bytes_returns_the_real_payload_for_a_usable_asset() -> None:
    business_id = BusinessId.new()
    asset = make_creative_asset(business_id=business_id)
    asset.mark_ready(PolicyVerdict(verdict=PolicyVerdictResult.PASS_, findings=()))
    lookup = _lookup(asset)

    payload = await lookup.get_bytes(business_id=business_id, asset_id=str(asset.asset_id))

    assert payload == _PNG_MAGIC_BYTES


async def test_get_bytes_of_another_business_is_none() -> None:
    asset = make_creative_asset(business_id=BusinessId.new())
    asset.mark_ready(PolicyVerdict(verdict=PolicyVerdictResult.PASS_, findings=()))
    lookup = _lookup(asset)

    payload = await lookup.get_bytes(business_id=BusinessId.new(), asset_id=str(asset.asset_id))

    assert payload is None


async def test_unknown_asset_id_is_none() -> None:
    lookup = _lookup(None)
    result = await lookup.find_usable(business_id=BusinessId.new(), asset_id=str(AssetId.new()))

    assert result is None


async def test_malformed_asset_id_is_none() -> None:
    lookup = _lookup(None)

    assert await lookup.find_usable(business_id=BusinessId.new(), asset_id="not-a-ulid") is None


async def test_asset_of_another_business_is_none() -> None:
    asset = make_creative_asset(business_id=BusinessId.new())
    asset.mark_ready(PolicyVerdict(verdict=PolicyVerdictResult.PASS_, findings=()))
    lookup = _lookup(asset)

    other_business = BusinessId.new()
    assert (
        await lookup.find_usable(business_id=other_business, asset_id=str(asset.asset_id)) is None
    )


async def test_asset_not_ready_is_none() -> None:
    business_id = BusinessId.new()
    asset = make_creative_asset(business_id=business_id)
    lookup = _lookup(asset)

    assert await lookup.find_usable(business_id=business_id, asset_id=str(asset.asset_id)) is None


async def test_asset_with_warn_verdict_is_none() -> None:
    business_id = BusinessId.new()
    asset = make_creative_asset(business_id=business_id)
    asset.mark_ready(PolicyVerdict(verdict=PolicyVerdictResult.WARN, findings=()))
    lookup = _lookup(asset)

    assert await lookup.find_usable(business_id=business_id, asset_id=str(asset.asset_id)) is None


async def test_non_image_media_kind_is_none_even_if_ready_and_passed() -> None:
    business_id = BusinessId.new()
    asset = make_creative_asset(business_id=business_id, media_kind=MediaKind.VIDEO)
    asset.mark_ready(PolicyVerdict(verdict=PolicyVerdictResult.PASS_, findings=()))
    lookup = _lookup(asset, payload=b"\x00\x00\x00\x18ftypmp42" + b"0" * 16)

    assert await lookup.find_usable(business_id=business_id, asset_id=str(asset.asset_id)) is None
