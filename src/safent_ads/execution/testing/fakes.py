"""Dobles en memoria de `execution/application/ports.py`."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime

from safent_ads.execution.application.execution_read_port import ExecutionView
from safent_ads.execution.application.ports import WriteCommand, WriteResult
from safent_ads.execution.application.single_execution_undo_port import SingleUndoResult
from safent_ads.execution.domain.execution_attempt import ExecutionAttempt, ExecutionStatus
from safent_ads.execution.domain.guardrails import (
    BrakeScope,
    BrakeScopeKind,
    EmergencyBrake,
    GuardrailScope,
    GuardrailSet,
    LedgerSnapshot,
    most_restrictive_brake,
)
from safent_ads.execution.domain.identifiers import ExecutionId
from safent_ads.proposals.domain.authorization import Authorization
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.proposal import ProposedDiff
from safent_ads.shared.events import DomainEvent
from safent_ads.shared.ids import EntityRef


class FakeExecutionQueuePort:
    """`claim_next` saca de una cola FIFO: una vez reclamado, ningun otro
    `claim_next` lo vuelve a devolver — modela "un unico worker" sin
    necesitar locks reales (T063: doble de `SELECT ... FOR UPDATE SKIP
    LOCKED`)."""

    def __init__(self) -> None:
        self._pending: deque[ExecutionAttempt] = deque()
        self.saved: dict[str, ExecutionAttempt] = {}
        self._by_proposal: dict[str, ExecutionAttempt] = {}

    def enqueue(self, attempt: ExecutionAttempt) -> None:
        self._pending.append(attempt)
        self._by_proposal[str(attempt.proposal_id)] = attempt

    async def claim_next(self, *, proposal_id: ProposalId | None = None) -> ExecutionAttempt | None:
        if proposal_id is None:
            if not self._pending:
                return None
            return self._pending.popleft()
        # Busca SOLO la fila de esa propuesta, sin tocar el orden de las
        # demas -- misma semantica que el `AND candidate.proposal_id = ...`
        # de `SqlExecutionQueue` (T075/F2-F3 C-2 nit 3).
        for index, attempt in enumerate(self._pending):
            if attempt.proposal_id == proposal_id:
                del self._pending[index]
                return attempt
        return None

    async def save(self, attempt: ExecutionAttempt) -> None:
        self.saved[str(attempt.execution_id)] = attempt
        self._by_proposal[str(attempt.proposal_id)] = attempt
        # Misma semantica que `SqlExecutionQueue.claim_next()`
        # (`outcome='CLAIMED' AND started_at IS NULL`): un intento nuevo,
        # todavia sin reclamar, se guarda reclamable -- quien crea la fila
        # (`AuthorizeRuleAction`+`RuleCycle`, `ApplyDefensiveAction`, la
        # aprobacion humana) llama `save()`, no `enqueue()` (ese metodo es
        # solo un atajo de arranque de test).
        if attempt.status is ExecutionStatus.CLAIMED and attempt.started_at is None:
            self._pending.append(attempt)

    async def get_for_proposal(self, proposal_id: ProposalId) -> ExecutionAttempt | None:
        return self._by_proposal.get(str(proposal_id))

    async def mark_undone_if_pending(
        self, execution_id: ExecutionId, *, now: datetime, reason: str
    ) -> bool:
        # Un unico `asyncio`/hilo por proceso de test: no hace falta el
        # candado de fila real de `SqlExecutionQueue`, solo el mismo
        # contrato -- comprobar `undone_at` y escribir son la MISMA
        # operacion (security-review-f4.md B-2).
        attempt = self.saved.get(str(execution_id))
        if attempt is None or attempt.undone_at is not None:
            return False
        attempt.undone_at = now
        attempt.undo_reason = reason
        return True

    async def attach_compensating_proposal(
        self, execution_id: ExecutionId, compensating_proposal_id: ProposalId
    ) -> None:
        attempt = self.saved.get(str(execution_id))
        if attempt is not None:
            attempt.compensating_proposal_id = compensating_proposal_id


class FakeUnitOfWork:
    """Registra el orden de entrada/salida en `calls` — los tests insertan
    sus propios marcadores en la misma lista (via `mark`) para probar que el
    guardarraíl se evalua DENTRO de la transaccion (threat-model.md C-15).

    `lock_account` no bloquea nada de verdad: un proceso de test es UN solo
    `asyncio` en un solo hilo, así que dos "transacciones-puerta" nunca
    corren de verdad a la vez -- la serializacion por cuenta (C-15/C-17) que
    `pg_advisory_xact_lock` da en Postgres solo hace falta probarla contra
    el adaptador SQL real (`tests/integration/execution/`). Aquí solo queda
    registrado en `calls`, para que los tests unitarios puedan comprobar
    que se pide ANTES de leer freno/guardarrailes."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def mark(self, label: str) -> None:
        self.calls.append(label)

    async def __aenter__(self) -> FakeUnitOfWork:
        self.calls.append("begin")
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.calls.append("commit" if exc_type is None else "rollback")

    async def lock_account(self, entity_ref: EntityRef) -> None:
        self.calls.append(f"lock_account:{entity_ref}")


class FakeAdsPlatformWritePort:
    """Idempotente por `idempotency_key`: una segunda llamada con la misma
    clave devuelve el resultado ya calculado sin incrementar `call_count`
    (T063: `test_retry_no_duplicate_change`)."""

    def __init__(
        self,
        *,
        confirmed_state_hash: str = "state-after",
        fail_with: Exception | None = None,
    ) -> None:
        self._confirmed_state_hash = confirmed_state_hash
        self._fail_with = fail_with
        self._results_by_key: dict[str, WriteResult] = {}
        self.call_count = 0
        self.calls: list[tuple[WriteCommand, Authorization, str]] = []

    async def execute_write(
        self, command: WriteCommand, authorization: Authorization, idempotency_key: str
    ) -> WriteResult:
        if idempotency_key in self._results_by_key:
            return self._results_by_key[idempotency_key]
        self.call_count += 1
        self.calls.append((command, authorization, idempotency_key))
        if self._fail_with is not None:
            raise self._fail_with
        result = WriteResult(
            applied_value=command.value, confirmed_state_hash=self._confirmed_state_hash
        )
        self._results_by_key[idempotency_key] = result
        return result

    async def read_receipt(
        self, command: WriteCommand, authorization: Authorization, idempotency_key: str
    ) -> WriteResult | None:
        del command, authorization
        return self._results_by_key.get(idempotency_key)


class FakeExecutionReservations:
    def __init__(self) -> None:
        self.active: dict[str, ProposedDiff] = {}

    async def reserve(self, attempt: ExecutionAttempt, effective: ProposedDiff) -> None:
        self.active[str(attempt.execution_id)] = effective

    async def get(self, attempt: ExecutionAttempt) -> ProposedDiff | None:
        return self.active.get(str(attempt.execution_id))

    async def resolve(self, attempt: ExecutionAttempt, *, applied: bool) -> None:
        del applied
        self.active.pop(str(attempt.execution_id), None)


class FakePlatformReaderPort:
    def __init__(
        self,
        *,
        state_hash_by_entity: dict[str, str] | None = None,
        fail: bool = False,
        on_fetch: Callable[[], None] | None = None,
    ) -> None:
        self._state_hash_by_entity = state_hash_by_entity or {}
        self._fail = fail
        # T075/F2-F3 C-18: hook para simular "el propietario pulsa el freno
        # justo aqui" -- la revalidacion de estado remoto es la unica ida y
        # vuelta de red entre el commit de `_pass_gates` y la escritura, asi
        # que es el punto exacto donde un banco puede colar el freno a
        # mitad de ciclo sin depender de temporizacion real.
        self._on_fetch = on_fetch

    async def fetch_state_hash(self, entity_ref: EntityRef) -> str:
        if self._on_fetch is not None:
            self._on_fetch()
        if self._fail:
            raise ConnectionError("plataforma inalcanzable")
        return self._state_hash_by_entity.get(str(entity_ref), "unknown")


class FakeBrakeStatePort:
    """`get` siempre lee del diccionario en vivo — nunca cachea (T061;
    threat-model.md C-18). Un test simula el freno activandose "a media
    ejecucion" llamando a `save` entre dos `run_once()`.

    `_business_by_account_ref` es el equivalente en memoria de la tabla
    `platform_accounts` que `SqlBrakeStatePort` consulta para resolver el
    negocio de una cuenta (`get_effective`): un doble sin base de datos no
    tiene de donde sacar esa relacion sola, asi que el test la declara con
    `link_account_to_business` -- sin ella, `get_effective` solo compone
    GLOBAL y el propio ambito de cuenta, igual que antes de este metodo."""

    def __init__(self) -> None:
        self._store: dict[str, EmergencyBrake] = {}
        self._business_by_account_ref: dict[str, str] = {}

    async def get(self, scope: BrakeScope) -> EmergencyBrake | None:
        return self._store.get(_brake_key(scope))

    async def get_effective(self, scope: BrakeScope) -> EmergencyBrake | None:
        brakes = [await self.get(candidate) for candidate in self._effective_candidates(scope)]
        return most_restrictive_brake(brakes)

    def _effective_candidates(self, scope: BrakeScope) -> list[BrakeScope]:
        global_scope = BrakeScope(kind=BrakeScopeKind.GLOBAL)
        if scope.kind is BrakeScopeKind.GLOBAL:
            return [scope]
        if scope.kind is BrakeScopeKind.BUSINESS:
            return [global_scope, scope]
        business_ref = self._business_by_account_ref.get(scope.ref or "")
        if business_ref is None:
            return [global_scope, scope]
        return [global_scope, BrakeScope(kind=BrakeScopeKind.BUSINESS, ref=business_ref), scope]

    async def save(self, brake: EmergencyBrake) -> None:
        self._store[_brake_key(brake.scope)] = brake

    def engage_now(self, brake: EmergencyBrake) -> None:
        """Version sincrona de `save` para hooks sincronos (p. ej.
        `FakePlatformReaderPort.on_fetch`) que simulan al propietario
        pulsando el freno a mitad de un `run_once()` en curso."""
        self._store[_brake_key(brake.scope)] = brake

    def link_account_to_business(self, account_ref: str, business_ref: str) -> None:
        """Registra que ambito `PLATFORM_ACCOUNT` (`account_ref`) cuelga de
        que negocio -- lo que en SQL resuelve el JOIN contra
        `platform_accounts`/`ad_entities`."""
        self._business_by_account_ref[account_ref] = business_ref


def _brake_key(scope: BrakeScope) -> str:
    return f"{scope.kind}:{scope.ref}"


class FakeSpendLedger:
    def __init__(self, snapshot_by_scope: dict[str, LedgerSnapshot] | None = None) -> None:
        self._snapshot_by_scope = snapshot_by_scope or {}
        self.recorded_changes: list[tuple[GuardrailScope, EntityRef, Money]] = []
        self.snapshot_queries: list[tuple[GuardrailScope, EntityRef]] = []

    async def snapshot(self, scope: GuardrailScope, entity_ref: EntityRef) -> LedgerSnapshot:
        self.snapshot_queries.append((scope, entity_ref))
        return self._snapshot_by_scope.get(
            scope.ref,
            LedgerSnapshot(
                platform_spend_today=Money.zero(),
                platform_spend_month_to_date=Money.zero(),
                applied_changes_today=Money.zero(),
                changes_count_today_for_entity=0,
            ),
        )

    async def record_applied_change(
        self, scope: GuardrailScope, entity_ref: EntityRef, delta: Money
    ) -> None:
        self.recorded_changes.append((scope, entity_ref, delta))


class FakeGuardrailSetRepository:
    def __init__(self, effective_by_scope: dict[str, GuardrailSet]) -> None:
        self._effective_by_scope = effective_by_scope

    async def get_effective(self, scope: GuardrailScope) -> GuardrailSet:
        return self._effective_by_scope[scope.ref]


class FakeRuleConditionPort:
    def __init__(self, live_check: Callable[[str, EntityRef], bool] | None = None) -> None:
        self._live_check = live_check or (lambda _rule_id, _entity_ref: True)

    async def is_condition_live(self, rule_id: str, entity_ref: EntityRef) -> bool:
        return self._live_check(rule_id, entity_ref)


class FakeFreshnessPort:
    def __init__(self, stale_check: Callable[[EntityRef], bool] | None = None) -> None:
        self._stale_check = stale_check or (lambda _entity_ref: False)

    async def is_stale(self, entity_ref: EntityRef) -> bool:
        return self._stale_check(entity_ref)


class FakeDecisionRecorder:
    """Implementa `shared.events.DecisionRecorder`. Registra TODO lo que se
    le pasa, incluidos los intentos fallidos (plan.md §6.7: "siempre, en
    exito y en fallo")."""

    def __init__(self) -> None:
        self.recorded: list[DomainEvent] = []

    async def record(self, event: DomainEvent) -> None:
        self.recorded.append(event)


class FakeExecutionReadPort:
    """Implementa `ExecutionReadPort` (execution.application) en memoria --
    `execution/presentation/rest.py` se prueba contra esto sin Postgres."""

    def __init__(self, views: list[ExecutionView] | None = None) -> None:
        self._views = list(views or ())

    async def list_for_business(
        self,
        business_id: str,
        *,
        outcome: str | None,
        since: datetime | None,
        limit: int,
    ) -> list[ExecutionView]:
        matching = [view for view in self._views if view.business_id == business_id]
        if outcome is not None:
            matching = [view for view in matching if view.outcome == outcome]
        if since is not None:
            matching = [view for view in matching if view.started_at >= since]
        return matching[:limit]

    async def get(self, execution_id: str) -> ExecutionView | None:
        for view in self._views:
            if view.execution_id == execution_id:
                return view
        return None


class FakeSingleExecutionUndoPort:
    """Implementa `SingleExecutionUndoPort` (execution.application):
    devuelve un resultado fijo por `execution_id`, configurado por el test."""

    def __init__(self, results: dict[str, SingleUndoResult] | None = None) -> None:
        self.results = dict(results or {})
        self.calls: list[tuple[str, str, str]] = []

    async def undo(self, *, execution_id: str, reason: str, initiated_by: str) -> SingleUndoResult:
        self.calls.append((execution_id, reason, initiated_by))
        return self.results.get(execution_id, SingleUndoResult(ok=False, error_code="NOT_FOUND"))
