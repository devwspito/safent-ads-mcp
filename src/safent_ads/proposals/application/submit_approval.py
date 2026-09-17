"""`SubmitApproval` (tasks.md T076/T083, contracts/rest-api.md `POST
/proposals/{id}/approve`): la aprobacion humana explicita -- panel y
Telegram comparten esta MISMA pieza (un solo camino de decision, nunca dos
implementaciones que puedan divergir).

Gracia servidora obligatoria (T083, spec.md pregunta 4: "20 s / 45 s"):
nunca `grace_period_seconds=0` para esta via -- a diferencia de una accion
`AUTO` (ya decidida por el motor de reglas), un `human_approval` recien
firmado SIGUE dando al propietario una ventana para arrepentirse antes de
que la fila sea reclamable, que es literalmente lo que la barra de
deshacer del panel mide."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from safent_ads.execution.application.ports import (
    BrakeStatePort,
    ExecutionQueuePort,
    GuardrailSetRepository,
)
from safent_ads.execution.domain.execution_attempt import ExecutionAttempt, build_idempotency_key
from safent_ads.execution.domain.guardrails import (
    BrakeMode,
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
from safent_ads.execution.domain.identifiers import ExecutionId
from safent_ads.iam.application.managed_ads_authority import ManagedAdsDenied, ManagedAdsUnavailable
from safent_ads.iam.application.managed_human_approval import (
    ManagedHumanApprovalPort,
    VerifiedManagedApproval,
)
from safent_ads.proposals.application.ports import AuthorizationRepository, ProposalRepository
from safent_ads.proposals.domain.authorization import (
    AuthorizationChannel,
    AuthorizationKind,
    SignerPort,
    sign_authorization,
)
from safent_ads.proposals.domain.campaign_creation import (
    CampaignCreationError,
    google_channel_from_creation_plan,
)
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.diff_hash import compute_diff_hash
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType
from safent_ads.proposals.domain.identifiers import AuthorizationId, ProposalId
from safent_ads.proposals.domain.proposal import Proposal, ProposalState
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import ApplicationError

__all__ = [
    "ApprovalDeniedReason",
    "ProposalApprovalDeniedError",
    "SubmitApproval",
    "SubmitApprovalCommand",
    "SubmitApprovalResult",
]

# spec.md pregunta abierta 4: nunca 0. `important`/`critical` reciben mas
# margen que `routine` porque su impacto ya paso el filtro de
# `ClassificationPolicy` -- Assumption documentada, pendiente de
# confirmacion del propietario (tasks.md T083 sigue sin marcar `[x]`).
_ROUTINE_GRACE = timedelta(seconds=20)
_ESCALATED_GRACE = timedelta(seconds=45)
_AUTHORIZATION_TTL = timedelta(minutes=15)


class ApprovalDeniedReason(StrEnum):
    DIFF_CHANGED = "DIFF_CHANGED"
    PROPOSAL_EXPIRED = "PROPOSAL_EXPIRED"
    PROPOSAL_NOT_PENDING = "PROPOSAL_NOT_PENDING"
    BRAKE_ENGAGED = "BRAKE_ENGAGED"
    GUARDRAIL_BLOCKED = "GUARDRAIL_BLOCKED"
    CAMPAIGN_PLAN_INVALID = "CAMPAIGN_PLAN_INVALID"
    CHANNEL_TYPE_NOT_ENABLED = "CHANNEL_TYPE_NOT_ENABLED"
    MANAGED_HUMAN_ADMISSION_REQUIRED = "MANAGED_HUMAN_ADMISSION_REQUIRED"
    MANAGED_HUMAN_ADMISSION_DENIED = "MANAGED_HUMAN_ADMISSION_DENIED"
    MANAGED_HUMAN_ADMISSION_UNAVAILABLE = "MANAGED_HUMAN_ADMISSION_UNAVAILABLE"


class ProposalApprovalDeniedError(ApplicationError):
    def __init__(self, reason: ApprovalDeniedReason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SubmitApprovalCommand:
    proposal_id: ProposalId
    diff_hash: str
    approved_by: str
    channel: AuthorizationChannel
    comment: str | None = None
    human_assertion: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class SubmitApprovalResult:
    authorization_id: str
    execution_id: str
    execution_scheduled_at: datetime
    grace_seconds: int


class SubmitApproval:
    def __init__(
        self,
        *,
        proposals: ProposalRepository,
        authorizations: AuthorizationRepository,
        execution_queue: ExecutionQueuePort,
        brakes: BrakeStatePort,
        guardrail_evaluator: GuardrailEvaluator,
        guardrail_sets: GuardrailSetRepository,
        spend_ledger: SpendLedger,
        signer: SignerPort,
        clock: Clock,
        human_authority: ManagedHumanApprovalPort | None = None,
        enabled_google_channels: frozenset[GoogleAdvertisingChannelType] = frozenset(
            {GoogleAdvertisingChannelType.SEARCH}
        ),
    ) -> None:
        self._proposals = proposals
        self._authorizations = authorizations
        self._execution_queue = execution_queue
        self._brakes = brakes
        self._guardrail_evaluator = guardrail_evaluator
        self._guardrail_sets = guardrail_sets
        self._spend_ledger = spend_ledger
        self._signer = signer
        self._clock = clock
        self._human_authority = human_authority
        self._enabled_google_channels = enabled_google_channels

    async def execute(self, command: SubmitApprovalCommand) -> SubmitApprovalResult:
        proposal = await self._require_pending(command)
        binding = proposal.diff.managed_binding
        managed = binding is not None
        if (
            managed
            and (
                self._human_authority is None
                or not command.human_assertion
                or command.channel != AuthorizationChannel.PANEL
            )
        ) or (not managed and command.human_assertion):
            raise ProposalApprovalDeniedError(
                ApprovalDeniedReason.MANAGED_HUMAN_ADMISSION_REQUIRED,
                "Enterprise human session admission is not enabled",
            )
        now = self._clock.now()
        self._require_not_expired(proposal, now)
        self._require_diff_matches(proposal, command)
        self._require_enabled_google_channel(proposal)

        scope = GuardrailScope(kind=ScopeKind.ENTITY, ref=str(proposal.diff.entity_ref))
        await self._require_brake_not_engaged(scope)
        verdict = await self._evaluate_guardrails(scope, proposal)
        if not verdict.allowed:
            raise ProposalApprovalDeniedError(
                ApprovalDeniedReason.GUARDRAIL_BLOCKED, ",".join(verdict.reasons)
            )

        approved_by = command.approved_by
        if managed:
            proof = await self._consume_managed(proposal, command, verdict)
            approved_by = str(proof.user_id)
            # The network roundtrip cannot freeze local state, brakes or policy.
            # A changed action burns this assertion; request new human consent.
            proposal = await self._require_pending(command)
            now = self._clock.now()
            self._require_not_expired(proposal, now)
            self._require_diff_matches(proposal, command)
            await self._require_brake_not_engaged(scope)
            verdict = await self._evaluate_guardrails(scope, proposal)
            fresh = effective_diff(proposal.diff, verdict)
            if (
                not verdict.allowed
                or fresh.managed_binding != binding
                or compute_diff_hash(
                    fresh.entity_ref, fresh.parameter, fresh.before, fresh.after, binding
                )
                != command.diff_hash
            ):
                raise ProposalApprovalDeniedError(
                    ApprovalDeniedReason.DIFF_CHANGED, "managed_action_changed"
                )
            now = self._clock.now()
            self._require_not_expired(proposal, now)
            if now.timestamp() >= proof.expires_at:
                raise ProposalApprovalDeniedError(
                    ApprovalDeniedReason.MANAGED_HUMAN_ADMISSION_DENIED,
                    "enterprise_consent_expired",
                )

        # BUG corregido (guardarrailes): firmar el diff EFECTIVO (recortado
        # si el veredicto recorto), nunca `proposal.diff.diff_hash` a
        # ciegas -- `proposal.diff` en si no se toca (INV-1). Sin recorte,
        # `effective_diff` devuelve el mismo diff y el hash no cambia.
        authorization = sign_authorization(
            authorization_id=AuthorizationId.new(),
            proposal_id=proposal.proposal_id,
            kind=AuthorizationKind.HUMAN_APPROVAL,
            proposal_classification=proposal.classification,
            diff_hash=effective_diff(proposal.diff, verdict).diff_hash,
            guardrail_verdict_hash=verdict.verdict_hash,
            issued_by=approved_by,
            channel=command.channel,
            decided_at=now,
            expires_at=now + _AUTHORIZATION_TTL,
            signer=self._signer,
            comment=None if managed else command.comment,
            managed_binding=proposal.diff.managed_binding,
        )
        await self._authorizations.save(authorization)

        # Dos `save()`, no uno: el trigger de `proposals` valida
        # transiciones de UN salto (PENDING->APPROVED->SCHEDULED, igual
        # que el chokepoint con SCHEDULED->EXECUTING -- ver su docstring
        # en `execution/application/chokepoint.py`). Un unico UPSERT que
        # saltase de PENDING a SCHEDULED lo rechazaria.
        proposal.approve(proposal.diff.diff_hash, now)
        await self._proposals.save(proposal)
        grace = _grace_for(proposal)
        scheduled_at = proposal.schedule_execution(int(grace.total_seconds()), now)
        await self._proposals.save(proposal)

        attempt = ExecutionAttempt(
            execution_id=ExecutionId.new(),
            business_id=proposal.business_id,
            proposal_id=proposal.proposal_id,
            authorization_id=authorization.authorization_id,
            idempotency_key=build_idempotency_key(proposal.proposal_id, proposal.diff.diff_hash),
            platform_state_hash_before=proposal.expected_state_hash,
        )
        await self._execution_queue.save(attempt)

        return SubmitApprovalResult(
            authorization_id=str(authorization.authorization_id),
            execution_id=str(attempt.execution_id),
            execution_scheduled_at=scheduled_at,
            grace_seconds=int(grace.total_seconds()),
        )

    async def _consume_managed(
        self, proposal: Proposal, command: SubmitApprovalCommand, verdict: GuardrailVerdict
    ) -> VerifiedManagedApproval:
        binding = proposal.diff.managed_binding
        authority = self._human_authority
        if binding is None or authority is None or not command.human_assertion:
            raise ProposalApprovalDeniedError(
                ApprovalDeniedReason.MANAGED_HUMAN_ADMISSION_REQUIRED,
                "enterprise_consent_required",
            )
        diff = effective_diff(proposal.diff, verdict)
        if (
            diff.diff_hash != command.diff_hash
            or compute_diff_hash(diff.entity_ref, diff.parameter, diff.before, diff.after, binding)
            != command.diff_hash
        ):
            raise ProposalApprovalDeniedError(
                ApprovalDeniedReason.DIFF_CHANGED, "managed_action_changed"
            )
        try:
            proof = await authority.consume(
                command.human_assertion,
                binding=binding,
                proposal_id=proposal.proposal_id.value,
                diff_hash=command.diff_hash,
            )
        except ManagedAdsDenied:
            raise ProposalApprovalDeniedError(
                ApprovalDeniedReason.MANAGED_HUMAN_ADMISSION_DENIED, "enterprise_consent_denied"
            ) from None
        except ManagedAdsUnavailable:
            raise ProposalApprovalDeniedError(
                ApprovalDeniedReason.MANAGED_HUMAN_ADMISSION_UNAVAILABLE,
                "enterprise_consent_unavailable",
            ) from None
        if (
            proof.binding != binding
            or proof.user_id != binding.user_id
            or proof.proposal_id != proposal.proposal_id.value
            or proof.diff_hash != command.diff_hash
            or self._clock.now().timestamp() >= proof.expires_at
        ):
            raise ProposalApprovalDeniedError(
                ApprovalDeniedReason.MANAGED_HUMAN_ADMISSION_DENIED, "enterprise_consent_mismatch"
            )
        return proof

    async def _require_pending(self, command: SubmitApprovalCommand) -> Proposal:
        proposal = await self._proposals.get(command.proposal_id)
        if proposal is None or proposal.state is not ProposalState.PENDING:
            raise ProposalApprovalDeniedError(
                ApprovalDeniedReason.PROPOSAL_NOT_PENDING, f"{command.proposal_id}"
            )
        return proposal

    def _require_not_expired(self, proposal: Proposal, now: datetime) -> None:
        if proposal.is_expired(now):
            raise ProposalApprovalDeniedError(
                ApprovalDeniedReason.PROPOSAL_EXPIRED, f"{proposal.proposal_id}"
            )

    def _require_diff_matches(self, proposal: Proposal, command: SubmitApprovalCommand) -> None:
        if command.diff_hash != proposal.diff.diff_hash:
            raise ProposalApprovalDeniedError(
                ApprovalDeniedReason.DIFF_CHANGED,
                f"pedido={command.diff_hash} vivo={proposal.diff.diff_hash}",
            )

    def _require_enabled_google_channel(self, proposal: Proposal) -> None:
        """T035 security re-check (CWE-284): `proposals.presentation.
        rest._edited_value` already rejects a PATCH that smuggles a
        disabled channel into `creation_plan`, but a proposal could reach
        approval through another writer of `proposals.after`, or this
        installation's `ADS_GOOGLE_CHANNELS_ENABLED` could have narrowed
        since the plan was proposed. Defence in depth, before the brake/
        guardrail checks -- pure, no port I/O."""
        if not proposal.diff.parameter.startswith("new_campaign:"):
            return
        after = proposal.diff.after
        creation_plan = after.get("creation_plan") if isinstance(after, dict) else None
        channel = google_channel_from_creation_plan(creation_plan)
        if channel is not None and channel not in self._enabled_google_channels:
            raise ProposalApprovalDeniedError(
                ApprovalDeniedReason.CHANNEL_TYPE_NOT_ENABLED, channel.value
            )

    async def _require_brake_not_engaged(self, scope: GuardrailScope) -> None:
        brake = await self._brakes.get_effective(brake_scope_from(scope))
        # `mode=ALL` bloquea tambien lo aprobado por humano (contracts/
        # rest-api.md §Freno); `AUTONOMOUS` solo frena `rule_authorization`
        # (`EmergencyBrake.blocks`), asi que una aprobacion humana sigue
        # adelante -- el chokepoint es quien de verdad decide en el reclamo.
        if brake is not None and brake.engaged and brake.mode is BrakeMode.ALL:
            raise ProposalApprovalDeniedError(ApprovalDeniedReason.BRAKE_ENGAGED, str(scope.ref))

    async def _evaluate_guardrails(
        self, scope: GuardrailScope, proposal: Proposal
    ) -> GuardrailVerdict:
        try:
            before, after = money_pair_from_diff(proposal.diff)
        except (CampaignCreationError, TypeError) as exc:
            raise ProposalApprovalDeniedError(
                ApprovalDeniedReason.CAMPAIGN_PLAN_INVALID,
                str(exc)
                if isinstance(exc, CampaignCreationError)
                else "campaign_creation_plan_invalid",
            ) from exc
        guardrails = await self._guardrail_sets.get_effective(scope)
        ledger = await self._spend_ledger.snapshot(scope, proposal.diff.entity_ref)
        change = GuardrailChange(
            scope=scope,
            entity_ref=proposal.diff.entity_ref,
            authorization_kind=AuthorizationKind.HUMAN_APPROVAL,
            before=before,
            after=after,
            is_creation=proposal.diff.parameter.startswith("new_campaign:"),
        )
        return self._guardrail_evaluator.evaluate(change, guardrails, ledger)


def _grace_for(proposal: Proposal) -> timedelta:
    if proposal.classification is Classification.ROUTINE:
        return _ROUTINE_GRACE
    return _ESCALATED_GRACE
