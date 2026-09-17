"""`ReplaceAdCreative` (tasks.md T026; contracts/api.md §6 `PATCH
/packages/{id}/ads/{ad_local_ref}/creative`): re-apunta la referencia de un
anuncio a otra creatividad YA verificada (negocio, `READY`, veredicto
`PASS`), recalcula la huella y, si habia una aprobacion viva, la invalida
(invariante 8)."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.packages.application.errors import (
    CreativeNotUsableError,
    PackageChangedError,
    PackageNotEditableError,
    PackageNotFoundError,
)
from safent_ads.packages.application.ports import CampaignPackageRepository, CreativeAssetLookupPort
from safent_ads.packages.domain.errors import CampaignPackageInvariantError, PlannedTreeError
from safent_ads.packages.domain.identifiers import PackageId
from safent_ads.packages.domain.planned_tree import AdRef
from safent_ads.packages.domain.values import ImageCreativeRef
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId

__all__ = ["ReplaceAdCreative", "ReplaceAdCreativeCommand"]


@dataclass(frozen=True, kw_only=True, slots=True)
class ReplaceAdCreativeCommand:
    business_id: BusinessId
    package_id: PackageId
    package_hash: str
    ad_local_ref: str
    creative_asset_id: str


class ReplaceAdCreative:
    def __init__(
        self,
        *,
        packages: CampaignPackageRepository,
        creative_lookup: CreativeAssetLookupPort,
        clock: Clock,
    ) -> None:
        self._packages = packages
        self._creative_lookup = creative_lookup
        self._clock = clock

    async def execute(self, command: ReplaceAdCreativeCommand) -> str:
        package = await self._packages.get(command.package_id, business_id=command.business_id)
        if package is None:
            raise PackageNotFoundError(str(command.package_id))
        if command.package_hash != package.package_hash.value:
            raise PackageChangedError(package.package_hash.value)

        snapshot = await self._creative_lookup.find_usable(
            business_id=command.business_id, asset_id=command.creative_asset_id
        )
        if snapshot is None:
            raise CreativeNotUsableError(ad_local_ref=command.ad_local_ref)
        creative = ImageCreativeRef(
            asset_id=AssetId.parse(snapshot.asset_id),
            checksum=snapshot.checksum,
            preview_key=snapshot.preview_key,
            mime_type=snapshot.mime_type,
            width=snapshot.width,
            height=snapshot.height,
        )

        try:
            ad_ref = AdRef(command.ad_local_ref)
        except PlannedTreeError as exc:
            raise PackageNotFoundError(command.ad_local_ref) from exc
        try:
            new_hash = package.replace_ad_creative(ad_ref, creative, self._clock.now())
        except CampaignPackageInvariantError as exc:
            if "no encontrado" in str(exc):
                raise PackageNotFoundError(str(exc)) from exc
            raise PackageNotEditableError(str(exc)) from exc
        await self._packages.save(package)
        return new_hash.value
