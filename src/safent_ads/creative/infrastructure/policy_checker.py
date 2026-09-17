"""`LocalPolicyChecker` implementa `PolicyCheckPort` (creative-port.md;
threat-model.md C-30): resuelve el `CreativeAsset` por `AssetRef` y aplica
las reglas puras de `domain/policy.py`. El unico I/O es la lectura del
activo — evaluar el texto sigue siendo una funcion pura."""

from __future__ import annotations

from safent_ads.creative.application.errors import (
    CreativeAssetMissingCopyError,
    CreativeAssetNotFoundError,
)
from safent_ads.creative.application.ports import CreativeAssetRepository
from safent_ads.creative.domain.enums import Placement
from safent_ads.creative.domain.identifiers import AssetRef
from safent_ads.creative.domain.policy import PolicyCheckContext, PolicyVerdict, evaluate_policy
from safent_ads.shared.ids import PlatformCode


class LocalPolicyChecker:
    def __init__(
        self,
        assets: CreativeAssetRepository,
        allowed_destination_domains: frozenset[str] = frozenset(),
    ) -> None:
        self._assets = assets
        self._allowed_destination_domains = allowed_destination_domains

    async def check(
        self, asset_ref: AssetRef, platform: PlatformCode, placement: Placement
    ) -> PolicyVerdict:
        asset = await self._assets.get(asset_ref)
        if asset is None:
            raise CreativeAssetNotFoundError(str(asset_ref))
        if asset.ad_copy is None:
            raise CreativeAssetMissingCopyError(str(asset_ref))
        context = PolicyCheckContext(
            platform=platform,
            placement=placement,
            ad_copy=asset.ad_copy,
            destination_url=asset.destination_url,
            allowed_destination_domains=self._allowed_destination_domains,
        )
        return evaluate_policy(context)
