"""Puertos de `proposals` que `execution` (N6) consume (plan.md §4/§5).

Los casos de uso de propuestas propiamente dichos (`SubmitApproval`,
`RejectProposal`, `GroupProposalsByCause`... tasks.md T076-T088) son de otro
lote de trabajo; estos puertos son el contrato minimo que necesita la rama
`us2` (T057-T070) para que `ExecutionChokepoint` y `AuthorizeRuleAction`
lean/escriban propuestas y autorizaciones sin acoplarse a una infraestructura
concreta."""

from __future__ import annotations

from typing import Protocol

from safent_ads.proposals.domain.authorization import Authorization
from safent_ads.proposals.domain.identifiers import AuthorizationId, ProposalId
from safent_ads.proposals.domain.proposal import Proposal
from safent_ads.shared.ids import EntityRef


class ProposalRepository(Protocol):
    async def get(self, proposal_id: ProposalId) -> Proposal | None: ...

    async def save(self, proposal: Proposal) -> None: ...

    async def find_live_equivalent(
        self, entity_ref: EntityRef, parameter: str
    ) -> Proposal | None:
        """FR-20: la propuesta abierta (estado vivo) para ese
        `(entity_ref, parameter)`, si la hay -- `ProposeAction` la actualiza
        en vez de duplicarla."""
        ...


class AuthorizationRepository(Protocol):
    async def get(self, authorization_id: AuthorizationId) -> Authorization | None: ...

    async def get_active_for_proposal(self, proposal_id: ProposalId) -> Authorization | None:
        """Ultima autorizacion con `decision = approved` para la propuesta,
        si existe (solo-anexable: revocar = anexar `decision = revoked`)."""
        ...

    async def save(self, authorization: Authorization) -> None: ...
