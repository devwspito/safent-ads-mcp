"""`ProposeCreative` (plan.md §5 `creative`): entrega un `CreativeAsset`
`READY` a `proposals` a traves de `ProposalGatewayPort`. Nunca publica
(creative-port.md: "Nada se publica desde aqui", FR-33) ni importa
`proposals` — la puerta de salida es la unica frontera (plan.md §4).

`POLICY_CHECK_REQUIRED` (rest-api.md) se comprueba AQUI, antes de mutar el
agregado: mas estricto que `CreativeAsset.propose()` (que solo exige "no
FAIL") porque la puerta REST de publicacion nunca deja pasar un `WARN` sin
que el propietario vuelva a revisar. Los `extra_asset_ids` se verifican
contra el mismo `business_id` que el activo principal — sin eso, un
`asset_id` de otro negocio se colaria en el `diff` de la propuesta
(IDOR)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from safent_ads.creative.application.errors import (
    CreativeAssetNotFoundError,
    CreativeAssetPolicyCheckRequiredError,
)
from safent_ads.creative.application.ports import (
    CreativeAssetRepository,
    CreativePublicationProposal,
    ProposalGatewayPort,
    ProposedAdCopy,
)
from safent_ads.creative.domain.creative_asset import CreativeAsset
from safent_ads.creative.domain.enums import PolicyVerdictResult
from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.shared.ids import BusinessId


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposeCreativeCommand:
    asset_id: AssetId
    ad_set_ref: str
    ad_copy: ProposedAdCopy
    extra_asset_ids: Sequence[AssetId] = field(default_factory=tuple)


class ProposeCreative:
    def __init__(
        self, proposal_gateway: ProposalGatewayPort, assets: CreativeAssetRepository
    ) -> None:
        self._proposal_gateway = proposal_gateway
        self._assets = assets

    async def execute(self, command: ProposeCreativeCommand) -> CreativePublicationProposal:
        asset = await self._require_asset(command.asset_id)
        self._require_policy_check_passed(asset)
        await self._require_extra_assets_same_business(command.extra_asset_ids, asset.business_id)
        asset.propose()
        result = await self._proposal_gateway.propose_creative_publication(
            asset_id=command.asset_id,
            business_id=asset.business_id,
            ad_set_ref=command.ad_set_ref,
            ad_copy=command.ad_copy,
            extra_asset_ids=command.extra_asset_ids,
        )
        await self._assets.update(asset)
        return result

    async def _require_asset(self, asset_id: AssetId) -> CreativeAsset:
        asset = await self._assets.get(asset_id)
        if asset is None:
            raise CreativeAssetNotFoundError(str(asset_id))
        return asset

    def _require_policy_check_passed(self, asset: CreativeAsset) -> None:
        verdict = asset.policy_verdict
        if verdict is None or verdict.verdict != PolicyVerdictResult.PASS_:
            raise CreativeAssetPolicyCheckRequiredError(
                f"{asset.asset_id} no tiene un PolicyVerdict PASS"
            )

    async def _require_extra_assets_same_business(
        self, extra_asset_ids: Sequence[AssetId], business_id: BusinessId
    ) -> None:
        for extra_id in extra_asset_ids:
            extra = await self._assets.get(extra_id)
            if extra is None or extra.business_id != business_id:
                raise CreativeAssetNotFoundError(str(extra_id))
