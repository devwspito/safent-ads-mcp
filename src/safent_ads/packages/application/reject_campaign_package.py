"""`RejectCampaignPackage` (contracts/api.md §4: `POST
/packages/{id}/reject`). Mismo par de comprobaciones que `approve` sobre la
huella -- rechazar tambien exige ver exactamente lo que se rechaza."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.packages.application.errors import (
    PackageChangedError,
    PackageNotFoundError,
    PackageNotProposedError,
)
from safent_ads.packages.application.ports import CampaignPackageRepository
from safent_ads.packages.domain.errors import CampaignPackageInvariantError
from safent_ads.packages.domain.identifiers import PackageId
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId

__all__ = ["RejectCampaignPackage", "RejectCampaignPackageCommand"]


@dataclass(frozen=True, kw_only=True, slots=True)
class RejectCampaignPackageCommand:
    business_id: BusinessId
    package_id: PackageId
    package_hash: str
    comment: str | None = None


class RejectCampaignPackage:
    def __init__(self, *, packages: CampaignPackageRepository, clock: Clock) -> None:
        self._packages = packages
        self._clock = clock

    async def execute(self, command: RejectCampaignPackageCommand) -> None:
        package = await self._packages.get(command.package_id, business_id=command.business_id)
        if package is None:
            raise PackageNotFoundError(str(command.package_id))
        if command.package_hash != package.package_hash.value:
            raise PackageChangedError(package.package_hash.value)
        try:
            package.reject(self._clock.now(), command.comment)
        except CampaignPackageInvariantError as exc:
            raise PackageNotProposedError(package.state.value) from exc
        await self._packages.save(package)
