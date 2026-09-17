"""`WithdrawProposal` (contracts/mcp-tools.md `withdraw_proposal`): el
agente retira su propia propuesta antes de que se ejecute. No es
`RejectProposal`/`InvalidateProposal` a secas -- la transicion de dominio
correcta depende del estado en que este viva (`PENDING`/`POSTPONED` se
rechazan; `APPROVED`/`SCHEDULED` ya tienen autorizacion viva y hay que
invalidarlos explicitamente, igual que hace `UndoExecution`).

`POSTPONED` no se retira directo (T076, alcance de esta pieza): la maquina
de estados exige reactivar primero (`POSTPONED -> PENDING`), y encadenar
esa reactivacion aqui seria decidir una politica de negocio ("un retiro
reactiva antes de rechazar") que `spec.md` no fija. Se deniega con un error
tipado en vez de inventarla."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.proposals.application.ports import ProposalRepository
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.domain.proposal import ProposalState
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import ApplicationError

__all__ = ["ProposalNotWithdrawableError", "WithdrawProposal", "WithdrawProposalCommand"]

_REJECTABLE = frozenset({ProposalState.PENDING})
_INVALIDATABLE = frozenset({ProposalState.APPROVED, ProposalState.SCHEDULED})


class ProposalNotWithdrawableError(ApplicationError):
    """La propuesta no existe o ya no esta en un estado del que se pueda
    retirar (ya resuelta, o `POSTPONED` -- ver docstring del modulo)."""


@dataclass(frozen=True, slots=True)
class WithdrawProposalCommand:
    proposal_id: ProposalId
    reason: str | None = None


class WithdrawProposal:
    def __init__(self, proposals: ProposalRepository, clock: Clock) -> None:
        self._proposals = proposals
        self._clock = clock

    async def execute(self, command: WithdrawProposalCommand) -> ProposalState:
        proposal = await self._proposals.get(command.proposal_id)
        if proposal is None:
            raise ProposalNotWithdrawableError(f"{command.proposal_id} no existe")
        now = self._clock.now()
        if proposal.state in _REJECTABLE:
            proposal.reject(now, command.reason)
        elif proposal.state in _INVALIDATABLE:
            proposal.invalidate(command.reason or "withdrawn_by_agent", now)
        else:
            raise ProposalNotWithdrawableError(
                f"{command.proposal_id} no se puede retirar en estado {proposal.state}"
            )
        await self._proposals.save(proposal)
        return proposal.state
