"""`ContainerCreativeProposalGateway`: implementa
`creative.application.ports.ProposalGatewayPort` (la UNICA puerta de
`creative` hacia `proposals`, plan.md §4) reutilizando la escritura real ya
cableada en `ContainerProposalWriteAdapter.propose_creative_publication`
(`composition/mcp_write_adapter.py`) -- "a traves de la aplicacion de
`proposals` ya existente", no una segunda tuberia.

Antes de delegar, resuelve el `ad_set_ref` UNA VEZ en una sesion propia de
solo lectura para comprobar que la entidad pertenece al MISMO
`business_id` que el activo creativo (IDOR, threat-model.md C-27):
`ContainerProposalWriteAdapter.propose_creative_publication` confia en que
quien lo llama (hasta ahora, solo el dispatcher MCP, que ya filtra por
`business_id_of` antes de despachar) ya hizo esa comprobacion -- esta es
la primera vez que un router REST lo invoca directamente, asi que la
comprobacion tiene que vivir aqui."""

from __future__ import annotations

from collections.abc import Sequence

from safent_ads.accounts.infrastructure.sql_repositories import SqlAdEntityRepository
from safent_ads.composition.container import Container
from safent_ads.composition.mcp_write_adapter import ContainerProposalWriteAdapter
from safent_ads.creative.application.errors import AdSetReferenceNotFoundError
from safent_ads.creative.application.ports import CreativePublicationProposal, ProposedAdCopy
from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.shared.ids import BusinessId, EntityRef, EntityRefFormatError

__all__ = ["ContainerCreativeProposalGateway"]

_CAUSE_TEXT = "Publicacion propuesta manualmente por el propietario desde el panel."


class ContainerCreativeProposalGateway:
    def __init__(self, container: Container) -> None:
        self._container = container
        self._write_port = ContainerProposalWriteAdapter(container)

    async def propose_creative_publication(
        self,
        *,
        asset_id: AssetId,
        business_id: BusinessId,
        ad_set_ref: str,
        ad_copy: ProposedAdCopy,
        extra_asset_ids: Sequence[AssetId],
    ) -> CreativePublicationProposal:
        await self._require_ad_set_owned_by(ad_set_ref, business_id)
        result = await self._write_port.propose_creative_publication(
            business_id=str(business_id),
            ad_set_ref=ad_set_ref,
            creative_asset_ids=(str(asset_id), *(str(extra_id) for extra_id in extra_asset_ids)),
            ad_copy={
                "headline": ad_copy.headline,
                "primary_text": ad_copy.primary_text,
                "cta": ad_copy.cta,
            },
            cause_text=_CAUSE_TEXT,
            cause_signal_id=None,
            cause_rule_id=None,
        )
        return CreativePublicationProposal(
            proposal_id=result.proposal_id,
            diff_hash=result.diff_hash,
            expires_at=result.expires_at,
        )

    async def _require_ad_set_owned_by(self, ad_set_ref: str, business_id: BusinessId) -> None:
        try:
            parsed_ref = EntityRef.parse(ad_set_ref)
        except EntityRefFormatError as exc:
            raise AdSetReferenceNotFoundError(ad_set_ref) from exc
        async with self._container.session_factory() as session:
            entity = await SqlAdEntityRepository(session).get_by_ref(parsed_ref)
        if entity is None or entity.business_id != business_id:
            raise AdSetReferenceNotFoundError(ad_set_ref)
