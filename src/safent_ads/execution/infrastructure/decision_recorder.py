"""`DecisionRecorder` (`shared/events.py`) real: anexa cada evento de dominio
de `proposals`/`execution` al `decision_log` solo-anexable a través de
`audit.application.record_decision.RecordDecision` -- el único punto de
entrada que ese contexto expone (plan.md §4: `execution` puede depender de
`audit`, nunca al revés).

`DecisionRecorder.record(event)` recibe el `DomainEvent` genérico que cada
agregado ya produce (`proposal.pull_events()`, `attempt.audit_event(now)`,
`EmergencyBrakeEngaged/Released`); este adaptador es la única pieza que sabe
traducir esa forma variada a un `PendingDecision` (`DecisionKind`,
`ActorKind`, payload sin PII -- `PendingDecision` ya rechaza claves
prohibidas). Reemplaza al `_NullDecisionRecorder` temporal de
`composition/container.py`."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Final

from safent_ads.audit.application.ports import DecisionLogRepository
from safent_ads.audit.application.record_decision import RecordDecision
from safent_ads.audit.domain.entry import ActorKind, DecisionKind, PendingDecision
from safent_ads.execution.domain.execution_attempt import ExecutionAttemptRecorded
from safent_ads.execution.domain.guardrails import EmergencyBrakeEngaged, EmergencyBrakeReleased
from safent_ads.proposals.domain.proposal import (
    ProposalApproved,
    ProposalExecuted,
    ProposalExpired,
    ProposalFailed,
    ProposalInvalidated,
    ProposalPostponed,
    ProposalRaised,
    ProposalRejected,
    ProposalSupersededByEquivalent,
)
from safent_ads.shared.events import DomainEvent
from safent_ads.shared.ids import BusinessId, EntityRef

__all__ = ["SqlDecisionRecorder", "UnmappedDecisionEventError"]

_KIND_BY_EVENT_TYPE: Final[dict[type[DomainEvent], DecisionKind]] = {
    ProposalRaised: DecisionKind.PROPOSAL,
    ProposalApproved: DecisionKind.APPROVAL,
    ProposalRejected: DecisionKind.APPROVAL,
    ProposalPostponed: DecisionKind.PROPOSAL,
    ProposalExpired: DecisionKind.PROPOSAL,
    ProposalInvalidated: DecisionKind.PROPOSAL,
    ProposalExecuted: DecisionKind.EXECUTION,
    ProposalFailed: DecisionKind.EXECUTION,
    ProposalSupersededByEquivalent: DecisionKind.PROPOSAL,
    ExecutionAttemptRecorded: DecisionKind.EXECUTION,
    EmergencyBrakeEngaged: DecisionKind.BRAKE,
    EmergencyBrakeReleased: DecisionKind.BRAKE,
}

# Estos eventos los produce el motor (regla/ejecución) o el propio proceso,
# nunca directamente una persona: `ActorKind.SYSTEM` es honesto -- quién
# aprobó/acuñó la autorización de fondo ya queda registrado en
# `approvals.issued_by`/`channel` (otra fila), este apunte del decision_log
# no lo duplica.
_ACTOR_KIND: Final[ActorKind] = ActorKind.SYSTEM

_ENTITY_REF_FIELDS: Final[tuple[str, ...]] = ("entity_ref",)
_PROPOSAL_ID_FIELDS: Final[tuple[str, ...]] = ("proposal_id",)
_IGNORED_PAYLOAD_FIELDS: Final[frozenset[str]] = frozenset(
    {"business_id", "occurred_at", "cycle_id", "entity_ref", "proposal_id"}
)


class UnmappedDecisionEventError(RuntimeError):
    """Un `DomainEvent` sin entrada en `_KIND_BY_EVENT_TYPE`: denegar por
    defecto en vez de adivinar un `DecisionKind` -- una entrada de
    `decision_log` mal clasificada es peor que una excepción visible."""


class SqlDecisionRecorder:
    """Implementa `shared.events.DecisionRecorder` sobre `RecordDecision`."""

    def __init__(self, repository: DecisionLogRepository) -> None:
        self._record_decision = RecordDecision(repository)

    async def record(self, event: DomainEvent) -> None:
        pending = _to_pending_decision(event)
        await self._record_decision.execute(pending)


def _to_pending_decision(event: DomainEvent) -> PendingDecision:
    kind = _KIND_BY_EVENT_TYPE.get(type(event))
    if kind is None:
        raise UnmappedDecisionEventError(
            f"sin DecisionKind para {type(event).__name__}: anadir a _KIND_BY_EVENT_TYPE"
        )
    fields = dataclasses.asdict(event)
    return PendingDecision(
        business_id=BusinessId.parse(str(event.business_id)),
        kind=kind,
        actor_kind=_ACTOR_KIND,
        payload={k: v for k, v in fields.items() if k not in _IGNORED_PAYLOAD_FIELDS},
        entity_ref=_optional_entity_ref(fields),
        proposal_id=_optional_proposal_id(fields),
    )


def _optional_entity_ref(fields: dict[str, object]) -> EntityRef | None:
    for name in _ENTITY_REF_FIELDS:
        value = fields.get(name)
        if isinstance(value, str):
            return EntityRef.parse(value)
    return None


def _optional_proposal_id(fields: dict[str, object]) -> uuid.UUID | None:
    for name in _PROPOSAL_ID_FIELDS:
        value = fields.get(name)
        if isinstance(value, str):
            return uuid.UUID(value)
    return None
