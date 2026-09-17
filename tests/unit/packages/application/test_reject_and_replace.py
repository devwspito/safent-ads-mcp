"""`RejectCampaignPackage` (contracts/api.md §4) y `ReplaceAdCreative`
(tasks.md T026)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.packages.application.errors import (
    CreativeNotUsableError,
    PackageChangedError,
    PackageNotFoundError,
    PackageNotProposedError,
)
from safent_ads.packages.application.ports import CreativeAssetSnapshot
from safent_ads.packages.application.reject_campaign_package import (
    RejectCampaignPackage,
    RejectCampaignPackageCommand,
)
from safent_ads.packages.application.replace_ad_creative import (
    ReplaceAdCreative,
    ReplaceAdCreativeCommand,
)
from safent_ads.packages.domain.campaign_package import PackageState
from safent_ads.packages.domain.identifiers import PackageId
from safent_ads.shared.clock import FixedClock

from ..domain.conftest import propose_meta_package
from .conftest import FakeCampaignPackageRepository, FakeCreativeAssetLookupPort

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)


def _asset_snapshot(asset_id: str) -> CreativeAssetSnapshot:
    return CreativeAssetSnapshot(
        asset_id=asset_id,
        checksum="b" * 64,
        mime_type="image/png",
        width=1200,
        height=628,
        preview_key="creative/preview-2.png",
    )


class TestRejectCampaignPackage:
    async def test_rejecting_a_proposed_package_transitions_it(self) -> None:
        package = propose_meta_package(now=NOW)
        repo = FakeCampaignPackageRepository()
        repo.by_id[str(package.package_id)] = package
        use_case = RejectCampaignPackage(packages=repo, clock=FixedClock(NOW))

        await use_case.execute(
            RejectCampaignPackageCommand(
                business_id=package.business_id,
                package_id=package.package_id,
                package_hash=package.package_hash.value,
                comment="No es el momento",
            )
        )

        assert package.state is PackageState.REJECTED

    async def test_reject_not_found(self) -> None:
        repo = FakeCampaignPackageRepository()
        use_case = RejectCampaignPackage(packages=repo, clock=FixedClock(NOW))

        with pytest.raises(PackageNotFoundError):
            await use_case.execute(
                RejectCampaignPackageCommand(
                    business_id=propose_meta_package(now=NOW).business_id,
                    package_id=PackageId.new(),
                    package_hash="a" * 64,
                )
            )

    async def test_reject_package_changed(self) -> None:
        package = propose_meta_package(now=NOW)
        repo = FakeCampaignPackageRepository()
        repo.by_id[str(package.package_id)] = package
        use_case = RejectCampaignPackage(packages=repo, clock=FixedClock(NOW))

        with pytest.raises(PackageChangedError):
            await use_case.execute(
                RejectCampaignPackageCommand(
                    business_id=package.business_id,
                    package_id=package.package_id,
                    package_hash="f" * 64,
                )
            )

    async def test_reject_already_rejected_is_not_proposed(self) -> None:
        package = propose_meta_package(now=NOW)
        repo = FakeCampaignPackageRepository()
        repo.by_id[str(package.package_id)] = package
        use_case = RejectCampaignPackage(packages=repo, clock=FixedClock(NOW))
        command = RejectCampaignPackageCommand(
            business_id=package.business_id,
            package_id=package.package_id,
            package_hash=package.package_hash.value,
        )
        await use_case.execute(command)

        with pytest.raises(PackageNotProposedError):
            await use_case.execute(command)


class TestReplaceAdCreative:
    async def test_replacing_creative_recalculates_the_hash(self) -> None:
        package = propose_meta_package(now=NOW)
        original_hash = package.package_hash.value
        repo = FakeCampaignPackageRepository()
        repo.by_id[str(package.package_id)] = package
        new_asset_id = str(AssetId.new())
        creative_lookup = FakeCreativeAssetLookupPort(
            usable={new_asset_id: _asset_snapshot(new_asset_id)}
        )
        use_case = ReplaceAdCreative(
            packages=repo, creative_lookup=creative_lookup, clock=FixedClock(NOW)
        )

        new_hash = await use_case.execute(
            ReplaceAdCreativeCommand(
                business_id=package.business_id,
                package_id=package.package_id,
                package_hash=original_hash,
                ad_local_ref="as#1/ad#1",
                creative_asset_id=new_asset_id,
            )
        )

        assert new_hash != original_hash
        assert package.package_hash.value == new_hash

    async def test_replace_with_unusable_creative(self) -> None:
        package = propose_meta_package(now=NOW)
        repo = FakeCampaignPackageRepository()
        repo.by_id[str(package.package_id)] = package
        use_case = ReplaceAdCreative(
            packages=repo,
            creative_lookup=FakeCreativeAssetLookupPort(usable={}),
            clock=FixedClock(NOW),
        )

        with pytest.raises(CreativeNotUsableError):
            await use_case.execute(
                ReplaceAdCreativeCommand(
                    business_id=package.business_id,
                    package_id=package.package_id,
                    package_hash=package.package_hash.value,
                    ad_local_ref="as#1/ad#1",
                    creative_asset_id=str(AssetId.new()),
                )
            )

    async def test_replace_on_unknown_ad_local_ref_is_not_found(self) -> None:
        package = propose_meta_package(now=NOW)
        repo = FakeCampaignPackageRepository()
        repo.by_id[str(package.package_id)] = package
        new_asset_id = str(AssetId.new())
        creative_lookup = FakeCreativeAssetLookupPort(
            usable={new_asset_id: _asset_snapshot(new_asset_id)}
        )
        use_case = ReplaceAdCreative(
            packages=repo, creative_lookup=creative_lookup, clock=FixedClock(NOW)
        )

        with pytest.raises(PackageNotFoundError):
            await use_case.execute(
                ReplaceAdCreativeCommand(
                    business_id=package.business_id,
                    package_id=package.package_id,
                    package_hash=package.package_hash.value,
                    ad_local_ref="as#1/ad#9",
                    creative_asset_id=new_asset_id,
                )
            )
