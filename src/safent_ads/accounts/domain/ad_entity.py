"""`AdEntity` (data-model.md): agregado por nivel (campana, conjunto/grupo,
anuncio, creatividad) de la jerarquia publicitaria. `EntityLevel` discrimina
el nivel; la forma es identica en Google y Meta (plan.md §5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from safent_ads.accounts.domain._aggregate import _EventRecordingAggregate
from safent_ads.accounts.domain.budget import Budget
from safent_ads.accounts.domain.errors import (
    InvalidEntityHierarchyError,
    InvalidStateTransitionError,
)
from safent_ads.accounts.domain.events import (
    AdEntityBudgetChanged,
    AdEntityDrifted,
    LearningPhaseCompleted,
    LearningPhaseEntered,
)
from safent_ads.accounts.domain.learning_state import LearningState
from safent_ads.accounts.domain.money import Money
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.shared.events import DomainEvent
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef


class AdEntityStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    REMOVED = "removed"
    DRIFTED = "drifted"
    LEARNING = "learning"


_CONTROLLABLE_LEVELS = frozenset(
    {EntityLevel.CAMPAIGN, EntityLevel.AD_SET, EntityLevel.AD, EntityLevel.CREATIVE}
)

_PARENT_LEVEL: dict[EntityLevel, EntityLevel] = {
    EntityLevel.CAMPAIGN: EntityLevel.ACCOUNT,
    EntityLevel.AD_SET: EntityLevel.CAMPAIGN,
    EntityLevel.AD: EntityLevel.AD_SET,
    EntityLevel.CREATIVE: EntityLevel.AD,
}


@dataclass(slots=True)
class AdEntity(_EventRecordingAggregate):
    business_id: BusinessId
    entity_ref: EntityRef
    parent_ref: EntityRef
    name: str
    status: AdEntityStatus
    platform_state_hash: PlatformStateHash
    is_controllable: bool
    learning_state: LearningState = field(default=LearningState.NOT_APPLICABLE)
    budget: Budget | None = field(default=None)
    bid_target: Money | None = field(default=None)
    shared_budget_ref: EntityRef | None = field(default=None)
    _pending_events: list[DomainEvent] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if (self.entity_ref.business_id, self.entity_ref.connection_id) != (
            self.parent_ref.business_id,
            self.parent_ref.connection_id,
        ) or self.entity_ref.business_id not in (None, self.business_id.value):
            raise InvalidEntityHierarchyError("entity_connection_scope_mismatch")
        if self.entity_ref.level not in _CONTROLLABLE_LEVELS:
            raise InvalidEntityHierarchyError(f"AdEntity no admite nivel {self.entity_ref.level}")
        expected_parent_level = _PARENT_LEVEL[self.entity_ref.level]
        if self.parent_ref.level != expected_parent_level:
            raise InvalidEntityHierarchyError(
                f"{self.entity_ref} requiere padre de nivel {expected_parent_level}, "
                f"recibido {self.parent_ref.level}"
            )

    @property
    def level(self) -> EntityLevel:
        return self.entity_ref.level

    def mark_drifted(self, observed_hash: PlatformStateHash, *, occurred_at: datetime) -> None:
        """data-model.md: divergencia de `platform_state_hash` ⇒ `drifted`,
        sin acciones. Idempotente: releer el mismo drift dos veces no emite
        un segundo evento."""
        if self.status == AdEntityStatus.DRIFTED:
            return
        self.status = AdEntityStatus.DRIFTED
        self._record_event(
            AdEntityDrifted(
                business_id=self.business_id,
                occurred_at=occurred_at,
                entity_ref=self.entity_ref,
                expected_hash=self.platform_state_hash,
                observed_hash=observed_hash,
            )
        )

    def change_budget(self, new_budget: Budget, *, occurred_at: datetime) -> None:
        if self.status == AdEntityStatus.REMOVED:
            raise InvalidStateTransitionError("entidad REMOVED no admite cambio de presupuesto")
        self._apply_budget(new_budget, occurred_at=occurred_at)

    def confirm_activation(self, confirmed_hash: PlatformStateHash) -> None:
        """`ACTIVATE_CAMPAIGN` (003-paquete-de-campana) confirmado por su
        propio recibo: la MISMA saga que la creo PAUSED (`RegisterCreatedEntity`)
        la sube a ACTIVE aqui, sin esperar al proximo sync periodico. Sin
        evento propio -- a diferencia de `mark_drifted`/`_apply_budget`,
        activar por el flujo normal de publicacion no es una anomalia que
        el dueño necesite ver, es el desenlace esperado."""
        self.status = AdEntityStatus.ACTIVE
        self.platform_state_hash = confirmed_hash

    def mark_removed_from_platform(self) -> None:
        """M1 (repaso 0.2.23): `SyncAccountInventory` ya no encuentra este
        `entity_ref` en el inventario remoto fresco -- se borro o se dio de
        baja fuera de este sistema. `proposals_entity_exists()` (0036) no
        puede seguir admitiendo `Proposal`s contra una fila que la
        plataforma ya no reconoce. Idempotente (releer la misma ausencia
        dos veces no hace nada); sin evento propio, igual que cualquier
        otro cambio de `status` de `refresh_from_platform`."""
        self.status = AdEntityStatus.REMOVED

    def refresh_from_platform(
        self,
        *,
        name: str,
        status: AdEntityStatus,
        is_controllable: bool,
        learning_state: LearningState,
        budget: Budget | None,
        bid_target: Money | None,
        shared_budget_ref: EntityRef | None,
        new_hash: PlatformStateHash,
        occurred_at: datetime,
    ) -> None:
        """`SyncAccountInventory` absorbe el estado remoto ya leido en una
        entidad conocida. No es una escritura gobernada: es la via por la
        que este sistema aprende la verdad (US1 = "ver la verdad").
        Solo emite eventos para las transiciones que tienen evento propio
        (presupuesto, fases de aprendizaje); el resto de campos se
        actualizan sin evento, igual que cualquier lectura de refresco."""
        self.name = name
        self.status = status
        self.is_controllable = is_controllable
        self.bid_target = bid_target
        self.shared_budget_ref = shared_budget_ref
        self.platform_state_hash = new_hash
        if budget is not None and budget != self.budget:
            self._apply_budget(budget, occurred_at=occurred_at)
        self._apply_learning_state(learning_state, occurred_at=occurred_at)

    def _apply_budget(self, new_budget: Budget, *, occurred_at: datetime) -> None:
        previous = self.budget
        self.budget = new_budget
        self._record_event(
            AdEntityBudgetChanged(
                business_id=self.business_id,
                occurred_at=occurred_at,
                entity_ref=self.entity_ref,
                previous_budget=previous,
                new_budget=new_budget,
            )
        )

    def _apply_learning_state(
        self, learning_state: LearningState, *, occurred_at: datetime
    ) -> None:
        was_learning = self.learning_state == LearningState.LEARNING
        if learning_state == self.learning_state:
            return
        if learning_state == LearningState.LEARNING:
            self.enter_learning(occurred_at=occurred_at)
            return
        if learning_state == LearningState.LEARNED and was_learning:
            self.complete_learning(occurred_at=occurred_at)
            return
        self.learning_state = learning_state

    def enter_learning(self, *, occurred_at: datetime) -> None:
        if self.learning_state == LearningState.LEARNING:
            return
        self.learning_state = LearningState.LEARNING
        self._record_event(
            LearningPhaseEntered(
                business_id=self.business_id, occurred_at=occurred_at, entity_ref=self.entity_ref
            )
        )

    def complete_learning(self, *, occurred_at: datetime) -> None:
        if self.learning_state != LearningState.LEARNING:
            raise InvalidStateTransitionError(
                f"{self.learning_state} -> LEARNED requiere estar en LEARNING"
            )
        self.learning_state = LearningState.LEARNED
        self._record_event(
            LearningPhaseCompleted(
                business_id=self.business_id, occurred_at=occurred_at, entity_ref=self.entity_ref
            )
        )
