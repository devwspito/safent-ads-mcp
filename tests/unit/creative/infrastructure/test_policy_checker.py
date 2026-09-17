"""`LocalPolicyChecker`: resuelve el activo por `AssetRef` y delega en
`domain.policy.evaluate_policy` (threat-model.md C-30; T106)."""

from __future__ import annotations

import asyncio

import pytest

from safent_ads.creative.application.errors import (
    CreativeAssetMissingCopyError,
    CreativeAssetNotFoundError,
)
from safent_ads.creative.domain.enums import Placement, PolicyVerdictResult
from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.creative.infrastructure.policy_checker import LocalPolicyChecker
from safent_ads.shared.ids import PlatformCode
from tests.unit.creative.domain.factories import make_ad_copy
from tests.unit.creative.infrastructure.fakes import (
    FakeCreativeAssetRepository,
    make_creative_asset,
)


def test_check_raises_when_asset_not_found() -> None:
    async def _run() -> None:
        checker = LocalPolicyChecker(FakeCreativeAssetRepository())
        await checker.check(AssetId.new(), PlatformCode.META, Placement.FEED)

    with pytest.raises(CreativeAssetNotFoundError):
        asyncio.run(_run())


def test_check_raises_when_asset_has_no_ad_copy() -> None:
    async def _run() -> None:
        asset = make_creative_asset()
        assets = FakeCreativeAssetRepository({asset.asset_id: asset})
        checker = LocalPolicyChecker(assets)
        await checker.check(asset.asset_id, PlatformCode.META, Placement.FEED)

    with pytest.raises(CreativeAssetMissingCopyError):
        asyncio.run(_run())


def test_check_delegates_to_evaluate_policy() -> None:
    async def _run() -> PolicyVerdictResult:
        asset = make_creative_asset(
            ad_copy=make_ad_copy(primary_text="Aprobado garantizado con nosotros.")
        )
        assets = FakeCreativeAssetRepository({asset.asset_id: asset})
        checker = LocalPolicyChecker(assets)
        verdict = await checker.check(asset.asset_id, PlatformCode.META, Placement.FEED)
        return verdict.verdict

    assert asyncio.run(_run()) == PolicyVerdictResult.FAIL


def test_check_uses_allowed_destination_domains() -> None:
    async def _run() -> PolicyVerdictResult:
        asset = make_creative_asset(
            ad_copy=make_ad_copy(), destination_url="https://evil.example/landing"
        )
        assets = FakeCreativeAssetRepository({asset.asset_id: asset})
        checker = LocalPolicyChecker(assets, allowed_destination_domains=frozenset({"ejemplo.es"}))
        verdict = await checker.check(asset.asset_id, PlatformCode.META, Placement.FEED)
        return verdict.verdict

    assert asyncio.run(_run()) == PolicyVerdictResult.FAIL
