"""`GetPlatformDivergence` (contracts/mcp-tools.md P1
`get_platform_divergence`): `delta_hat` traduce, nunca corrige gasto -- el
salto frente al periodo anterior es lo que marca la anomalia de medicion."""

from __future__ import annotations

from safent_ads.economics.application.dto import PlatformDivergenceView
from safent_ads.economics.application.errors import PlatformDivergenceNotFoundError
from safent_ads.economics.application.ports import PlatformDivergenceRepository
from safent_ads.shared.ids import BusinessId


class GetPlatformDivergence:
    def __init__(self, divergences: PlatformDivergenceRepository) -> None:
        self._divergences = divergences

    async def execute(
        self, *, business_id: BusinessId, platform_account_id: str
    ) -> PlatformDivergenceView:
        latest = await self._divergences.get_latest(
            business_id=business_id, platform_account_id=platform_account_id
        )
        if latest is None:
            raise PlatformDivergenceNotFoundError(platform_account_id)
        previous = await self._divergences.get_previous(
            business_id=business_id, platform_account_id=platform_account_id
        )
        return PlatformDivergenceView(
            platform_account_id=platform_account_id,
            crm_conversions=latest.crm_conversions,
            platform_conversions=latest.platform_conversions,
            value=latest.value,
            is_outside_sanity_band=latest.is_outside_sanity_band,
            is_jump_anomaly=latest.is_jump_anomaly(previous) if previous is not None else None,
        )
