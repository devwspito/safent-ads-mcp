"""Puertos que `ExecutionChokepoint` y los casos de uso de `execution`
consumen (plan.md §5/§6). Cada uno tiene un doble en `execution/testing/fakes.py`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from safent_ads.execution.domain.execution_attempt import ExecutionAttempt
from safent_ads.execution.domain.guardrails import (
    BrakeScope,
    EmergencyBrake,
    GuardrailScope,
    GuardrailSet,
    SpendLedger,
)
from safent_ads.execution.domain.identifiers import ExecutionId
from safent_ads.proposals.domain.authorization import Authorization
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.shared.ids import EntityRef
from safent_ads.shared.managed_ads import ManagedAdsBinding

__all__ = [
    "AdsPlatformWritePort",
    "BrakeStatePort",
    "ExecutionQueuePort",
    "FreshnessPort",
    "GuardrailSetRepository",
    "PlatformReaderPort",
    "RuleConditionPort",
    "SpendLedger",
    "UnitOfWork",
    "WriteCommand",
    "WriteResult",
]


class ExecutionQueuePort(Protocol):
    """Paso 1 de plan.md §6: `SELECT ... FOR UPDATE SKIP LOCKED`. El
    adaptador real reclama una fila `executions` lista para ejecutar; el
    doble en memoria (`execution/testing/fakes.py`) modela "reclamado por un
    unico worker" marcando la fila como entregada."""

    async def claim_next(self, *, proposal_id: ProposalId | None = None) -> ExecutionAttempt | None:
        """Sin `proposal_id`: la fila reclamable mas antigua de CUALQUIER
        negocio (`ExecutionCycle`/`RuleCycle`, un worker de cola). Con
        `proposal_id`: solo esa fila, si sigue reclamable -- lo que necesita
        `ApplyDefensiveAction` (C-2, security review F2/F3 nit 3): el agente
        pidio ejecutar SU propuesta, no la que lleve mas tiempo esperando de
        otro negocio."""
        ...

    async def save(self, attempt: ExecutionAttempt) -> None: ...

    async def get_for_proposal(self, proposal_id: ProposalId) -> ExecutionAttempt | None:
        """Ultimo intento (cualquier estado) de una propuesta — usado por
        `UndoExecution` (T070) para localizar la `undo_deadline`."""
        ...

    async def mark_undone_if_pending(
        self, execution_id: ExecutionId, *, now: datetime, reason: str
    ) -> bool:
        """Escritura CONDICIONAL y atomica (security-review-f4.md B-2):
        equivalente a `UPDATE executions SET undone_at = :now, undo_reason
        = :reason WHERE id = :execution_id AND undone_at IS NULL`. Devuelve
        `True` si ESTA llamada es la que reclamo el deshacer; `False` si
        otra transaccion ya lo habia marcado antes -- el llamador
        (`UndoExecution`) debe tratar `False` como `ExecutionAlreadyUndoneError`,
        nunca reintentar. Deliberadamente separado de `save()` (UPSERT
        incondicional de toda la fila): `save()` no basta para cerrar la
        carrera porque no comprueba el estado previo antes de escribir."""
        ...

    async def attach_compensating_proposal(
        self, execution_id: ExecutionId, compensating_proposal_id: ProposalId
    ) -> None:
        """Segundo paso del marcado de deshacer (B-2), deliberadamente
        separado de `mark_undone_if_pending`: `executions.
        compensating_proposal_id` tiene FK a `proposals.id` y esa fila
        todavia no existe cuando el guard atomico de arriba corre -- crearla
        antes del guard dejaria una propuesta huerfana si el guard
        rechazase el deshacer por duplicado. Se llama SIEMPRE dentro de la
        MISMA transaccion que `mark_undone_if_pending`, justo despues de
        `proposals.save(compensating)`."""
        ...


class UnitOfWork(Protocol):
    """Frontera transaccional (threat-model.md C-15: "guardarraíl evaluado
    DENTRO de la transaccion de reclamo"). El adaptador real envuelve una
    sesion SQLAlchemy; el doble registra el orden de entrada/salida."""

    async def __aenter__(self) -> UnitOfWork: ...

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None: ...

    async def lock_account(self, entity_ref: EntityRef) -> None:
        """Serializa por CUENTA (security review F2/F3, C-15/C-17): un
        `pg_advisory_xact_lock` de transaccion, tomado ANTES de evaluar
        guardarrailes, para que dos transacciones-puerta concurrentes sobre
        la MISMA cuenta nunca lean el ledger a la vez. Se libera solo con el
        commit/rollback de la transaccion que la llamo -- nunca hace falta
        soltarlo a mano."""
        ...


@dataclass(frozen=True, slots=True)
class WriteCommand:
    """El diff EFECTIVO (recortado por guardarrailes si toca) que el
    chokepoint ya verifico y firmo -- `before` viaja explicito (BUG
    corregido: `BrokerPlatformWriter` clasificaba RAISE/LOWER desde
    `proposal.diff.before/after`, el diff CRUDO, en vez de este mismo
    `before` vs `value`; si un ambito ya estaba fuera de suelo/techo por
    deriva externa, el recorte podia invertir la direccion real del cambio
    sin que la infraestructura se enterase). El adaptador de plataforma
    nunca debe recomputar `before` releyendo la propuesta -- este campo es
    la unica fuente de verdad para clasificar la operacion."""

    entity_ref: EntityRef
    parameter: str
    before: object
    value: object
    managed_binding: ManagedAdsBinding | None = None


@dataclass(frozen=True, slots=True)
class WriteResult:
    applied_value: object
    confirmed_state_hash: str
    # `003-paquete-de-campana`: el recurso de plataforma que una escritura
    # de CREACION confirma (`campaign_resource`/`child_resource`,
    # `WriteOutcome.platform_request_id`) -- se perdia en esta frontera
    # (`applied_value` es el diff firmado, nunca el id creado). Solo lo
    # rellenan `CREATE_CAMPAIGN`/`CREATE_AD_SET`/`CREATE_AD`; `None` en
    # cualquier otra escritura, como hoy.
    created_external_id: str | None = None


class AdsPlatformWritePort(Protocol):
    """Paso 6 de plan.md §6: unico punto que habla con el broker. Incluye la
    lectura de confirmacion post-escritura en `confirmed_state_hash`
    (FR-21: "verificar estado real tras aplicar")."""

    async def execute_write(
        self, command: WriteCommand, authorization: Authorization, idempotency_key: str
    ) -> WriteResult: ...

    async def read_receipt(
        self, command: WriteCommand, authorization: Authorization, idempotency_key: str
    ) -> WriteResult | None:
        """Read an immutable prior receipt; never renew authority or perform a mutation."""
        ...


class ConfirmedWriteRejection(Exception):
    """A verified broker rejection before mutation, not a generic FAILED/timeout."""


class PlatformReaderPort(Protocol):
    """Usado por `PlatformStateRevalidator` (paso 5 de plan.md §6)."""

    async def fetch_state_hash(self, entity_ref: EntityRef) -> str: ...


class BrakeStatePort(Protocol):
    """T061: se lee en CADA ruta de ejecucion, nunca cacheado
    (threat-model.md C-18)."""

    async def get(self, scope: BrakeScope) -> EmergencyBrake | None: ...

    async def save(self, brake: EmergencyBrake) -> None: ...

    async def get_effective(self, scope: BrakeScope) -> EmergencyBrake | None:
        """El freno que de verdad decide una escritura sobre `scope`
        (`PLATFORM_ACCOUNT`, bug corregido): combina GLOBAL, BUSINESS (el
        negocio dueño de la cuenta) y el propio `scope` con
        `guardrails.most_restrictive_brake` -- `get()` solo mira una fila
        y por eso sigue siendo lo correcto para el interruptor
        (`ToggleEmergencyBrake`) y las vistas de solo lectura, que ya
        operan sobre un ambito concreto y conocido de antemano."""
        ...


class RuleConditionPort(Protocol):
    """T067: pregunta al motor de reglas (otra rama) si la condicion de una
    regla esta disparando AHORA sobre una entidad. Puerto local para no
    acoplar `execution` a `rules/` antes de que exista."""

    async def is_condition_live(self, rule_id: str, entity_ref: EntityRef) -> bool: ...


class FreshnessPort(Protocol):
    """`STALE_DATA` de `contracts/mcp-tools.md`. Puerto local hacia
    `metrics/data_freshness` (otra rama)."""

    async def is_stale(self, entity_ref: EntityRef) -> bool: ...


class GuardrailSetRepository(Protocol):
    """Devuelve el `GuardrailSet` YA compuesto (ambito especifico sobre el
    general, via `GuardrailSet.effective_with`) para un ambito — el
    chokepoint nunca compone el resultado el mismo."""

    async def get_effective(self, scope: GuardrailScope) -> GuardrailSet: ...
