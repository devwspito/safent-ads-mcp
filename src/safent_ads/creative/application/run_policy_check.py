"""`RunPolicyCheck` (plan.md §5 `creative`; contracts/mcp-tools.md
`run_creative_policy_check`): verifica un activo contra las normas de una
plataforma/emplazamiento y aplica el veredicto sobre el agregado
(DRAFT -> READY | REJECTED)."""

from __future__ import annotations

from safent_ads.creative.application.errors import CreativeAssetNotFoundError
from safent_ads.creative.application.ports import CreativeAssetRepository, PolicyCheckPort
from safent_ads.creative.domain.enums import Placement
from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.creative.domain.policy import PolicyVerdict
from safent_ads.shared.ids import PlatformCode


class RunPolicyCheck:
    def __init__(self, policy_checker: PolicyCheckPort, assets: CreativeAssetRepository) -> None:
        self._policy_checker = policy_checker
        self._assets = assets

    async def execute(
        self, asset_id: AssetId, platform: PlatformCode, placement: Placement
    ) -> PolicyVerdict:
        asset = await self._assets.get(asset_id)
        if asset is None:
            raise CreativeAssetNotFoundError(str(asset_id))
        verdict = await self._policy_checker.check(asset_id, platform, placement)
        asset.mark_ready(verdict)
        await self._assets.update(asset)
        return verdict
