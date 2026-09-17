"""`Proposal` — agregado raiz del contexto `proposals` (data-model.md tabla
`proposals`; T057). Puerto de `oposads/hitl/domain/propuesta.py::PropuestaDeAccion`
con nombres en ingles y un estado `SCHEDULED` anadido para la ventana de
gracia servidora (FR-15, `execution_scheduled_at`).

Invariantes que este agregado protege:
1. `diff_hash` se recalcula desde el payload vivo (`diff_hash.py`), nunca se
   confia en el almacenado.
2. Editar `after` (valor propuesto) invalida cualquier autorizacion previa:
   si la propuesta ya estaba `APPROVED`/`SCHEDULED`, vuelve a `PENDING` y
   emite `ProposalInvalidated`.
3. Una propuesta caducada nunca transiciona a ejecucion.
4. Maquina de 9 estados de oposads + `SCHEDULED`."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from safent_ads.proposals.domain.cause import Cause, CauseKey, Evidence
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.diff_hash import compute_diff_hash
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Priority
from safent_ads.shared.errors import DomainError
from safent_ads.shared.events import DomainEvent
from safent_ads.shared.ids import BusinessId, EntityRef
from safent_ads.shared.managed_ads import ManagedAdsBinding


class ProposalInvariantError(DomainError):
    """Se intento una transicion de estado invalida o una precondicion no se
    cumplio (p. ej. editar una propuesta ya ejecutada)."""


class DiffChangedError(DomainError):
    """El `diff_hash` con el que se intenta aprobar/autorizar no coincide con
    el `diff_hash` vivo del agregado (invariante 2)."""


class ProposalState(StrEnum):
    PENDING = "pending"
    POSTPONED = "postponed"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED = "failed"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    REJECTED = "rejected"


class PostponedReason(StrEnum):
    """data-model.md `Proposal.PostponedReason`: `owner` (POST
    `/proposals/{id}/postpone`, decision explicita) o `attention_budget`
    (el excedente del presupuesto de atencion nace pospuesto, NFR-11 --
    todavia sin cablear en esta rama)."""

    OWNER = "owner"
    ATTENTION_BUDGET = "attention_budget"


# data-model.md `OwnerContext`: "texto libre del propietario, <=500".
MAX_OWNER_CONTEXT_LENGTH = 500


_TRANSITIONS: dict[ProposalState, frozenset[ProposalState]] = {
    ProposalState.PENDING: frozenset(
        {
            ProposalState.APPROVED,
            ProposalState.REJECTED,
            ProposalState.EXPIRED,
            ProposalState.POSTPONED,
        }
    ),
    ProposalState.POSTPONED: frozenset({ProposalState.PENDING, ProposalState.EXPIRED}),
    ProposalState.APPROVED: frozenset({ProposalState.SCHEDULED, ProposalState.INVALIDATED}),
    ProposalState.SCHEDULED: frozenset({ProposalState.EXECUTING, ProposalState.INVALIDATED}),
    ProposalState.EXECUTING: frozenset({ProposalState.EXECUTED, ProposalState.FAILED}),
    ProposalState.REJECTED: frozenset(),
    ProposalState.EXPIRED: frozenset(),
    ProposalState.EXECUTED: frozenset(),
    ProposalState.FAILED: frozenset(),
    ProposalState.INVALIDATED: frozenset(),
}

# Estados en los que la propuesta ya tiene (o tuvo) una autorizacion viva;
# editar el valor propuesto desde aqui debe invalidarla explicitamente.
_AUTHORIZED_STATES: frozenset[ProposalState] = frozenset(
    {ProposalState.APPROVED, ProposalState.SCHEDULED}
)


@dataclass(frozen=True, slots=True)
class ProposedDiff:
    """El cambio exacto propuesto para un parametro de una entidad
    (data-model.md `ProposedDiff`; `diff_hash` ligado al payload)."""

    entity_ref: EntityRef
    parameter: str
    before: object
    after: object
    diff_hash: str
    managed_binding: ManagedAdsBinding | None = None

    @classmethod
    def build(
        cls,
        entity_ref: EntityRef,
        parameter: str,
        before: object,
        after: object,
        *,
        managed_binding: ManagedAdsBinding | None = None,
    ) -> ProposedDiff:
        diff_hash = compute_diff_hash(entity_ref, parameter, before, after, managed_binding)
        return cls(entity_ref, parameter, before, after, diff_hash, managed_binding)

    def with_new_value(self, new_after: object) -> ProposedDiff:
        return ProposedDiff.build(
            self.entity_ref,
            self.parameter,
            self.before,
            new_after,
            managed_binding=self.managed_binding,
        )


# ---------------------------------------------------------------------------
# Domain events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ProposalRaised(DomainEvent):
    proposal_id: str
    entity_ref: str
    classification: str


@dataclass(frozen=True, kw_only=True)
class ProposalApproved(DomainEvent):
    proposal_id: str
    diff_hash: str


@dataclass(frozen=True, kw_only=True)
class ProposalRejected(DomainEvent):
    proposal_id: str
    reason: str | None = None


@dataclass(frozen=True, kw_only=True)
class ProposalPostponed(DomainEvent):
    proposal_id: str
    postpone_until: str


@dataclass(frozen=True, kw_only=True)
class ProposalExpired(DomainEvent):
    proposal_id: str


@dataclass(frozen=True, kw_only=True)
class ProposalInvalidated(DomainEvent):
    proposal_id: str
    reason: str


@dataclass(frozen=True, kw_only=True)
class ProposalExecuted(DomainEvent):
    proposal_id: str


@dataclass(frozen=True, kw_only=True)
class ProposalFailed(DomainEvent):
    proposal_id: str
    error: str


@dataclass(frozen=True, kw_only=True)
class ProposalSupersededByEquivalent(DomainEvent):
    proposal_id: str
    absorbed_cause: str


# ---------------------------------------------------------------------------
# Aggregate root
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Proposal:
    """Agregado raiz — la unidad de supervision humana (data-model.md)."""

    proposal_id: ProposalId
    business_id: BusinessId
    diff: ProposedDiff
    classification: Classification
    cause: Cause
    cause_key: CauseKey
    evidence: tuple[Evidence, ...]
    estimated_impact: Money
    priority: Priority
    created_at: datetime
    expires_at: datetime
    state: ProposalState = ProposalState.PENDING
    expected_state_hash: str | None = None
    postpone_until: datetime | None = None
    postponed_reason: PostponedReason | None = None
    execution_scheduled_at: datetime | None = None
    expected_contribution_delta: Money | None = None
    owner_context: str | None = None
    # 004 tasks.md A8: `person:<user_id>` cuando una llamada MCP con puesto
    # crea la propuesta, `None` cuando la crea el motor de reglas o el
    # dueno desde el panel. Dato de PROCEDENCIA, no de autorizacion: no
    # participa en `diff_hash` ni en la firma de aprobacion
    # (`proposals/domain/authorization.py`); cambiarlo no puede invalidar
    # ni habilitar una aprobacion.
    proposed_by: str | None = None
    _events: list[DomainEvent] = field(default_factory=list)

    @classmethod
    def raise_proposal(
        cls,
        *,
        proposal_id: ProposalId,
        business_id: BusinessId,
        diff: ProposedDiff,
        classification: Classification,
        cause: Cause,
        cause_key: CauseKey,
        evidence: tuple[Evidence, ...],
        estimated_impact: Money,
        priority: Priority,
        now: datetime,
        expires_at: datetime,
        expected_state_hash: str | None = None,
        expected_contribution_delta: Money | None = None,
        proposed_by: str | None = None,
    ) -> Proposal:
        proposal = cls(
            proposal_id=proposal_id,
            business_id=business_id,
            diff=diff,
            classification=classification,
            cause=cause,
            cause_key=cause_key,
            evidence=evidence,
            estimated_impact=estimated_impact,
            priority=priority,
            created_at=now,
            expires_at=expires_at,
            expected_state_hash=expected_state_hash,
            expected_contribution_delta=expected_contribution_delta,
            proposed_by=proposed_by,
        )
        proposal._events.append(
            ProposalRaised(
                business_id=business_id,
                occurred_at=now,
                proposal_id=str(proposal_id),
                entity_ref=str(diff.entity_ref),
                classification=classification.value,
            )
        )
        return proposal

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def approve(self, authorized_diff_hash: str, now: datetime) -> None:
        """PENDING -> APPROVED. Invariante 2: el hash autorizado debe coincidir
        con el hash vivo del diff."""
        self._require_state(ProposalState.PENDING)
        if authorized_diff_hash != self.diff.diff_hash:
            raise DiffChangedError(
                f"authorized diff_hash {authorized_diff_hash!r} != "
                f"live diff_hash {self.diff.diff_hash!r}"
            )
        self._transition_to(ProposalState.APPROVED)
        self._events.append(
            ProposalApproved(
                business_id=self.business_id,
                occurred_at=now,
                proposal_id=str(self.proposal_id),
                diff_hash=self.diff.diff_hash,
            )
        )

    def reject(self, now: datetime, reason: str | None = None) -> None:
        self._require_state(ProposalState.PENDING)
        self._transition_to(ProposalState.REJECTED)
        self._events.append(
            ProposalRejected(
                business_id=self.business_id,
                occurred_at=now,
                proposal_id=str(self.proposal_id),
                reason=reason,
            )
        )

    def expire(self, now: datetime) -> None:
        self._require_state(ProposalState.PENDING, ProposalState.POSTPONED)
        self._transition_to(ProposalState.EXPIRED)
        self._events.append(
            ProposalExpired(
                business_id=self.business_id, occurred_at=now, proposal_id=str(self.proposal_id)
            )
        )

    def postpone(
        self,
        postpone_until: datetime,
        now: datetime,
        reason: PostponedReason = PostponedReason.OWNER,
    ) -> None:
        self._require_state(ProposalState.PENDING)
        if postpone_until <= now:
            raise ProposalInvariantError("postpone_until debe ser futuro")
        self._transition_to(ProposalState.POSTPONED)
        self.postpone_until = postpone_until
        self.postponed_reason = reason
        self._events.append(
            ProposalPostponed(
                business_id=self.business_id,
                occurred_at=now,
                proposal_id=str(self.proposal_id),
                postpone_until=postpone_until.isoformat(),
            )
        )

    def reactivate(self, now: datetime) -> None:
        """POSTPONED -> PENDING, o EXPIRED si `expires_at` ya paso."""
        self._require_state(ProposalState.POSTPONED)
        self.postpone_until = None
        self.postponed_reason = None
        if self.is_expired(now):
            self._transition_to(ProposalState.EXPIRED)
            self._events.append(
                ProposalExpired(
                    business_id=self.business_id,
                    occurred_at=now,
                    proposal_id=str(self.proposal_id),
                )
            )
        else:
            self._transition_to(ProposalState.PENDING)

    def edit_proposed_value(self, new_after: object, now: datetime) -> ProposedDiff:
        """Cambia `after`; recalcula `diff_hash`. Si ya habia una autorizacion
        viva (`APPROVED`/`SCHEDULED`), la invalida explicitamente volviendo a
        `PENDING` y emitiendo `ProposalInvalidated` — cualquier `Authorization`
        emitida para el hash anterior dejara de verificar (`authorization.py`)."""
        if self.state not in (ProposalState.PENDING, *_AUTHORIZED_STATES):
            raise ProposalInvariantError(f"no se puede editar una propuesta en estado {self.state}")
        previous_state = self.state
        self.diff = self.diff.with_new_value(new_after)
        if previous_state in _AUTHORIZED_STATES:
            self.state = ProposalState.PENDING
            self.execution_scheduled_at = None
            self._events.append(
                ProposalInvalidated(
                    business_id=self.business_id,
                    occurred_at=now,
                    proposal_id=str(self.proposal_id),
                    reason=f"edited_while_{previous_state.value}",
                )
            )
        return self.diff

    def set_owner_context(self, text: str) -> None:
        """El propietario deja una nota libre sobre la propuesta
        (data-model.md `OwnerContext`, PUT `/proposals/{id}/owner-context`).
        No es una transicion de estado -- no depende del estado vivo de la
        maquina, solo protege el limite de longitud del propio agregado."""
        if len(text) > MAX_OWNER_CONTEXT_LENGTH:
            raise ProposalInvariantError(
                f"owner_context supera el maximo de {MAX_OWNER_CONTEXT_LENGTH} caracteres"
            )
        self.owner_context = text

    def update_with_equivalent(
        self,
        *,
        new_diff: ProposedDiff,
        new_cause: Cause,
        new_evidence: tuple[Evidence, ...],
        new_estimated_impact: Money,
        new_expires_at: datetime,
        now: datetime,
    ) -> None:
        """FR-20: una sola propuesta abierta por `(entity_ref, parameter)` —
        el ciclo que produce otra equivalente actualiza esta en vez de crear
        una nueva. Solo valido mientras no hay autorizacion viva."""
        self._require_state(ProposalState.PENDING, ProposalState.POSTPONED)
        if new_diff.managed_binding != self.diff.managed_binding:
            raise ProposalInvariantError("managed_binding_change_requires_new_proposal")
        self.diff = new_diff
        self.cause = new_cause
        self.evidence = new_evidence
        self.estimated_impact = new_estimated_impact
        self.expires_at = new_expires_at
        if self.state is ProposalState.POSTPONED:
            self.state = ProposalState.PENDING
            self.postpone_until = None
            self.postponed_reason = None
        self._events.append(
            ProposalSupersededByEquivalent(
                business_id=self.business_id,
                occurred_at=now,
                proposal_id=str(self.proposal_id),
                absorbed_cause=new_cause.text,
            )
        )

    def schedule_execution(self, grace_period_seconds: int, now: datetime) -> datetime:
        """APPROVED -> SCHEDULED. Fija `execution_scheduled_at` = fin de la
        ventana de gracia (FR-15; T083: gracia servidora real, nunca 0 salvo
        que se pida explicitamente)."""
        self._require_state(ProposalState.APPROVED)
        self._transition_to(ProposalState.SCHEDULED)
        self.execution_scheduled_at = now + timedelta(seconds=grace_period_seconds)
        return self.execution_scheduled_at

    def begin_execution(self, now: datetime) -> None:
        """SCHEDULED -> EXECUTING. Rechaza si la ventana de gracia no paso o
        si la propuesta ya caduco (invariante 3)."""
        self._require_state(ProposalState.SCHEDULED)
        if self.is_expired(now):
            raise ProposalInvariantError("no se ejecuta una propuesta caducada")
        if self.execution_scheduled_at is not None and now < self.execution_scheduled_at:
            raise ProposalInvariantError("la ventana de gracia no ha pasado")
        self._transition_to(ProposalState.EXECUTING)

    def record_execution(self, success: bool, now: datetime, error: str | None = None) -> None:
        self._require_state(ProposalState.EXECUTING)
        if success:
            self._transition_to(ProposalState.EXECUTED)
            self._events.append(
                ProposalExecuted(
                    business_id=self.business_id,
                    occurred_at=now,
                    proposal_id=str(self.proposal_id),
                )
            )
        else:
            self._transition_to(ProposalState.FAILED)
            self._events.append(
                ProposalFailed(
                    business_id=self.business_id,
                    occurred_at=now,
                    proposal_id=str(self.proposal_id),
                    error=error or "unknown",
                )
            )

    def invalidate(self, reason: str, now: datetime) -> None:
        """APPROVED|SCHEDULED -> INVALIDATED (deriva de plataforma, recorte de
        guardarrailes que cambia el `diff_hash`, etc.)."""
        self._require_state(*_AUTHORIZED_STATES)
        self._transition_to(ProposalState.INVALIDATED)
        self._events.append(
            ProposalInvalidated(
                business_id=self.business_id,
                occurred_at=now,
                proposal_id=str(self.proposal_id),
                reason=reason,
            )
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def is_equivalent_to(self, other: Proposal) -> bool:
        """FR-20: misma entidad + mismo parametro = la misma "cosa" pendiente
        de decision, independientemente de la causa que la origino."""
        return (
            self.diff.entity_ref == other.diff.entity_ref
            and self.diff.parameter == other.diff.parameter
        )

    def pull_events(self) -> list[DomainEvent]:
        events, self._events = self._events, []
        return events

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _transition_to(self, target: ProposalState) -> None:
        allowed = _TRANSITIONS.get(self.state, frozenset())
        if target not in allowed:
            raise ProposalInvariantError(f"transicion invalida {self.state} -> {target}")
        self.state = target

    def _require_state(self, *states: ProposalState) -> None:
        if self.state not in states:
            raise ProposalInvariantError(f"se esperaba estado en {states}, es {self.state}")


def new_proposal_id() -> ProposalId:
    return ProposalId(uuid.uuid4())
