"""`AuthorizeRuleAction` (T067) — acuña una `Authorization` de tipo
`rule_authorization` SOLO si la condicion de la regla esta disparando ahora
mismo sobre la entidad Y el veredicto de guardarrailes en vivo lo permite
(`contracts/mcp-tools.md::apply_defensive_action`, comprobaciones 1-4).

El servidor no confia en el agente: recalcula todo desde datos vivos, nunca
acepta un veredicto o una condicion que el llamador afirme haber
comprobado ya."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from safent_ads.execution.application.ports import (
    BrakeStatePort,
    GuardrailSetRepository,
    RuleConditionPort,
)
from safent_ads.execution.domain.guardrails import (
    GuardrailChange,
    GuardrailEvaluator,
    GuardrailScope,
    GuardrailVerdict,
    ScopeKind,
    SpendLedger,
    brake_scope_from,
    effective_diff,
    money_pair_from_diff,
)
from safent_ads.proposals.application.ports import AuthorizationRepository, ProposalRepository
from safent_ads.proposals.domain.authorization import (
    Authorization,
    AuthorizationChannel,
    AuthorizationKind,
    SignerPort,
    sign_authorization,
)
from safent_ads.proposals.domain.identifiers import AuthorizationId, ProposalId
from safent_ads.proposals.domain.proposal import Proposal, ProposalState
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import ApplicationError
from safent_ads.shared.ids import EntityRef

_AUTHORIZATION_TTL = timedelta(minutes=15)


class RuleAuthorizationDenialReason(StrEnum):
    """Subconjunto de `contracts/mcp-tools.md` que aplica a este caso de uso."""

    RULE_NOT_APPLICABLE = "RULE_NOT_APPLICABLE"
    BRAKE_ENGAGED = "BRAKE_ENGAGED"
    GUARDRAIL_BLOCKED = "GUARDRAIL_BLOCKED"


class RuleAuthorizationDeniedError(ApplicationError):
    def __init__(self, reason: RuleAuthorizationDenialReason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AuthorizeRuleActionCommand:
    proposal_id: ProposalId
    rule_id: str


class AuthorizeRuleAction:
    def __init__(
        self,
        *,
        proposals: ProposalRepository,
        authorizations: AuthorizationRepository,
        rule_condition: RuleConditionPort,
        brakes: BrakeStatePort,
        guardrail_evaluator: GuardrailEvaluator,
        guardrail_sets: GuardrailSetRepository,
        spend_ledger: SpendLedger,
        signer: SignerPort,
        clock: Clock,
    ) -> None:
        self._proposals = proposals
        self._authorizations = authorizations
        self._rule_condition = rule_condition
        self._brakes = brakes
        self._guardrail_evaluator = guardrail_evaluator
        self._guardrail_sets = guardrail_sets
        self._spend_ledger = spend_ledger
        self._signer = signer
        self._clock = clock

    async def execute(self, command: AuthorizeRuleActionCommand) -> Authorization:
        proposal = await self._require_pending_proposal(command.proposal_id)
        if proposal.diff.managed_binding is not None:
            raise RuleAuthorizationDeniedError(
                RuleAuthorizationDenialReason.RULE_NOT_APPLICABLE, "managed human approval required"
            )
        await self._require_live_condition(command.rule_id, proposal.diff.entity_ref)

        scope = GuardrailScope(kind=ScopeKind.ENTITY, ref=str(proposal.diff.entity_ref))
        await self._require_brake_not_engaged(scope)
        verdict = await self._evaluate_guardrails(scope, proposal)
        if not verdict.allowed:
            raise RuleAuthorizationDeniedError(
                RuleAuthorizationDenialReason.GUARDRAIL_BLOCKED, ",".join(verdict.reasons)
            )

        return await self._mint(command.rule_id, proposal, verdict)

    async def _require_pending_proposal(self, proposal_id: ProposalId) -> Proposal:
        proposal = await self._proposals.get(proposal_id)
        if proposal is None or proposal.state is not ProposalState.PENDING:
            raise RuleAuthorizationDeniedError(
                RuleAuthorizationDenialReason.RULE_NOT_APPLICABLE, "proposal not pending"
            )
        return proposal

    async def _require_live_condition(self, rule_id: str, entity_ref: EntityRef) -> None:
        is_live = await self._rule_condition.is_condition_live(rule_id, entity_ref)
        if not is_live:
            raise RuleAuthorizationDeniedError(
                RuleAuthorizationDenialReason.RULE_NOT_APPLICABLE, "rule condition not live"
            )

    async def _require_brake_not_engaged(self, scope: GuardrailScope) -> None:
        brake = await self._brakes.get_effective(brake_scope_from(scope))
        if brake is not None and brake.blocks(AuthorizationKind.RULE_AUTHORIZATION):
            raise RuleAuthorizationDeniedError(
                RuleAuthorizationDenialReason.BRAKE_ENGAGED, "emergency brake engaged"
            )

    async def _evaluate_guardrails(
        self, scope: GuardrailScope, proposal: Proposal
    ) -> GuardrailVerdict:
        guardrails = await self._guardrail_sets.get_effective(scope)
        ledger = await self._spend_ledger.snapshot(scope, proposal.diff.entity_ref)
        before, after = money_pair_from_diff(proposal.diff)
        change = GuardrailChange(
            scope=scope,
            entity_ref=proposal.diff.entity_ref,
            authorization_kind=AuthorizationKind.RULE_AUTHORIZATION,
            before=before,
            after=after,
            is_creation=proposal.diff.parameter.startswith("new_campaign:"),
        )
        return self._guardrail_evaluator.evaluate(change, guardrails, ledger)

    async def _mint(
        self, rule_id: str, proposal: Proposal, verdict: GuardrailVerdict
    ) -> Authorization:
        now = self._clock.now()
        # BUG corregido (guardarrailes): firmar el diff EFECTIVO (recortado
        # si el veredicto recorto), nunca `proposal.diff.diff_hash` a
        # ciegas -- `proposal.diff` en si no se toca (INV-1). Sin recorte,
        # `effective_diff` devuelve el mismo diff y el hash no cambia.
        authorization = sign_authorization(
            authorization_id=AuthorizationId.new(),
            proposal_id=proposal.proposal_id,
            kind=AuthorizationKind.RULE_AUTHORIZATION,
            proposal_classification=proposal.classification,
            diff_hash=effective_diff(proposal.diff, verdict).diff_hash,
            guardrail_verdict_hash=verdict.verdict_hash,
            issued_by=rule_id,
            channel=AuthorizationChannel.RULE_ENGINE,
            decided_at=now,
            expires_at=now + _AUTHORIZATION_TTL,
            signer=self._signer,
            managed_binding=proposal.diff.managed_binding,
        )
        await self._authorizations.save(authorization)
        proposal.approve(proposal.diff.diff_hash, now)
        await self._proposals.save(proposal)
        return authorization
