"""`ProposeAction` (tasks.md T081/T068): construye y persiste una
`PropuestaDeAccion` `pendiente` a partir de un cambio propuesto ya resuelto
por el llamador. Nunca toca una plataforma (contracts/mcp-tools.md regla 3).

Dos llamadores comparten esta pieza: los `propose_*`/`withdraw_proposal` de
MCP (el agente ya resolvio `entity_ref`/`parameter`/`before`/`after`) y
`RuleCycle` (el motor de reglas hace lo mismo desde una `Signal`). Ninguno
de los dos decide AQUI si hace falta aprobacion humana o autonomia: eso lo
fija `ClassificationPolicy` (FR-12) y, para las reglas `AUTO`, lo
autoriza despues `AuthorizeRuleAction` -- este caso de uso solo sabe crear
o actualizar la propuesta (FR-20: una equivalente actualiza, nunca
duplica)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from safent_ads.proposals.application.ports import ProposalRepository
from safent_ads.proposals.domain.ad_child_creation import (
    AdChildCreationError,
    child_parameter,
    validate_child_diff,
)
from safent_ads.proposals.domain.cause import Cause, CauseKey, Evidence
from safent_ads.proposals.domain.classification import (
    Classification,
    ClassificationPolicy,
    ProposalKind,
)
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import ExpiryPolicy, Urgency
from safent_ads.proposals.domain.priority import Priority as ProposalPriority
from safent_ads.proposals.domain.proposal import (
    Proposal,
    ProposalInvariantError,
    ProposalState,
    ProposedDiff,
    new_proposal_id,
)
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId

__all__ = ["ProposeAction", "ProposeActionCommand", "ProposeActionResult"]


@dataclass(frozen=True, slots=True)
class ProposeActionCommand:
    business_id: BusinessId
    diff: ProposedDiff
    kind: ProposalKind
    cause: Cause
    cause_type: str
    evidence: tuple[Evidence, ...]
    estimated_impact: Money
    urgency: Urgency
    calendar_event_id: str | None = None
    expected_state_hash: str | None = None
    # 004 tasks.md A8: `person:<user_id>` de la llamada MCP que origino
    # esta propuesta, `None` para el motor de reglas.
    proposed_by: str | None = None


@dataclass(frozen=True, slots=True)
class ProposeActionResult:
    proposal_id: ProposalId
    diff_hash: str
    state: ProposalState
    classification: Classification
    expires_at: datetime


class ProposeAction:
    def __init__(
        self,
        *,
        proposals: ProposalRepository,
        classification_policy: ClassificationPolicy,
        expiry_policy: ExpiryPolicy,
        clock: Clock,
    ) -> None:
        self._proposals = proposals
        self._classification_policy = classification_policy
        self._expiry_policy = expiry_policy
        self._clock = clock

    async def execute(self, command: ProposeActionCommand) -> ProposeActionResult:
        if child_parameter(command.diff.parameter):
            plan = validate_child_diff(
                command.diff.parameter,
                command.diff.before,
                command.diff.after,
                command.diff.entity_ref,
                command.expected_state_hash,
            )
            if command.kind.value != f"create_{plan['kind']}":
                raise AdChildCreationError
        now = self._clock.now()
        classification = self._classification_policy.classify(
            command.kind, command.estimated_impact
        )
        expires_at = self._expiry_policy.expires_at(command.urgency, now)
        existing = await self._proposals.find_live_equivalent(
            command.diff.entity_ref, command.diff.parameter
        )
        if existing is not None:
            if existing.diff.managed_binding != command.diff.managed_binding:
                raise ProposalInvariantError("physical_action_conflict")
            if existing.business_id != command.business_id:
                raise ProposalInvariantError("physical_action_business_mismatch")
            if existing.diff.entity_ref != command.diff.entity_ref or existing.state not in (
                ProposalState.PENDING,
                ProposalState.POSTPONED,
            ):
                if (
                    existing.diff.before != command.diff.before
                    or existing.diff.after != command.diff.after
                ):
                    # Different requests are not equivalent: the existing action
                    # must be reviewed/edited, never silently reported as accepted.
                    raise ProposalInvariantError("physical_action_conflict")
                return _result(existing, existing.classification)
            existing.update_with_equivalent(
                new_diff=command.diff,
                new_cause=command.cause,
                new_evidence=command.evidence,
                new_estimated_impact=command.estimated_impact,
                new_expires_at=expires_at,
                now=now,
            )
            await self._proposals.save(existing)
            return _result(existing, classification)

        proposal = Proposal.raise_proposal(
            proposal_id=new_proposal_id(),
            business_id=command.business_id,
            diff=command.diff,
            classification=classification,
            cause=command.cause,
            cause_key=CauseKey(
                entity_ref=command.diff.entity_ref,
                rule_id=command.cause.rule_id or "agent",
                cause_type=command.cause_type,
            ),
            evidence=command.evidence,
            estimated_impact=command.estimated_impact,
            priority=ProposalPriority(
                urgency=command.urgency, calendar_event_id=command.calendar_event_id
            ),
            now=now,
            expires_at=expires_at,
            expected_state_hash=command.expected_state_hash,
            proposed_by=command.proposed_by,
        )
        await self._proposals.save(proposal)
        return _result(proposal, classification)


def _result(proposal: Proposal, classification: Classification) -> ProposeActionResult:
    return ProposeActionResult(
        proposal_id=proposal.proposal_id,
        diff_hash=proposal.diff.diff_hash,
        state=proposal.state,
        classification=classification,
        expires_at=proposal.expires_at,
    )
