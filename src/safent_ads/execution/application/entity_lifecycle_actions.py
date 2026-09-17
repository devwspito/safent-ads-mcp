"""`PauseEntity`/`ResumeEntity`/`DeleteEntity` (design.md del panel
simple, §0.5-0.7): el propietario pausa, reanuda o borra una entidad
directamente desde el panel, sin pasar por una propuesta que otro tenga que
aprobar despues -- el clic ES la autorizacion humana explicita
(`human_approval`, canal `PANEL`, exactamente igual que `SubmitApproval`).

Mismo camino que una propuesta ya aprobada (`ExecutionChokepoint`, plan.md
§6): se firma una `Authorization`, se programa con gracia cero (accion
inmediata) y se ejecuta ya, sincronamente, contra el MISMO chokepoint --
igual que `ApplyDefensiveAction._propose_authorize_and_run`, solo que aqui
la autorizacion es `human_approval` (el propietario decide) en vez de
`rule_authorization` (el motor de reglas). El intento queda en
`executions`, auditado; pausar/reanudar admite `POST
/executions/{id}/undo` dentro de `UndoGracePolicy.pause_grace`; borrar
nunca (`UndoGracePolicy.grace_for` devuelve `None` para una transicion a
`DELETED`, y `UndoExecution` ya rechaza deshacer un intento sin ventana).

`parameter="status"` con valores LITERALES (`ACTIVE`/`PAUSED`/`DELETED`),
nunca `Money` -- a diferencia de `ApplyDefensiveAction`/
`orchestration.rule_step` (que modelan una pausa como presupuesto
equivalente para el guardarraíl). Esta es la forma que
`broker.domain.operation_semantics.matches_signed_transition` y
`execution.infrastructure.broker_platform._operation` ya esperan para
clasificar PAUSE/RESUME/DELETE en la escritura real (ver sus docstrings,
y `tests/unit/broker/platforms/test_meta_ads_adapter.py::
test_execute_write_pauses_a_campaign`, que ya firma un `WriteIntent` con
`before="ACTIVE"`/`after="PAUSED"` literales). `money_pair_from_diff`
proyecta cualquier valor no-`Money` a `Money.zero()`, asi que el
guardarraíl nunca ve un cambio de gasto real por una accion de puro
status (Assumption documentada)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from safent_ads.accounts.application.ports import AdEntityRepository
from safent_ads.accounts.domain.ad_entity import AdEntity, AdEntityStatus
from safent_ads.execution.application.chokepoint import ExecutionChokepoint
from safent_ads.execution.application.ports import (
    BrakeStatePort,
    ExecutionQueuePort,
    GuardrailSetRepository,
)
from safent_ads.execution.domain.execution_attempt import (
    ExecutionAttempt,
    ExecutionStatus,
    build_idempotency_key,
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
from safent_ads.execution.domain.identifiers import ExecutionId
from safent_ads.proposals.application.ports import AuthorizationRepository, ProposalRepository
from safent_ads.proposals.application.propose_action import ProposeAction, ProposeActionCommand
from safent_ads.proposals.domain.authorization import (
    AuthorizationChannel,
    AuthorizationKind,
    SignerPort,
    sign_authorization,
)
from safent_ads.proposals.domain.cause import Cause
from safent_ads.proposals.domain.classification import ProposalKind
from safent_ads.proposals.domain.identifiers import AuthorizationId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Urgency
from safent_ads.proposals.domain.proposal import ProposedDiff
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import ApplicationError
from safent_ads.shared.ids import BusinessId, EntityRef

__all__ = [
    "DeleteEntity",
    "DeleteEntityCommand",
    "EntityActionDenialCode",
    "EntityActionDeniedError",
    "EntityActionResult",
    "EntityLifecycleDeps",
    "PauseEntity",
    "PauseEntityCommand",
    "ResumeEntity",
    "ResumeEntityCommand",
    "UnknownEntityError",
]

_AUTHORIZATION_TTL = timedelta(minutes=15)
_STATUS_PARAMETER = "status"
_ACTIVE_VALUE = "ACTIVE"
_PAUSED_VALUE = "PAUSED"
_DELETED_VALUE = "DELETED"


class EntityActionDenialCode(StrEnum):
    BRAKE_ENGAGED = "BRAKE_ENGAGED"
    NOT_CONTROLLABLE = "NOT_CONTROLLABLE"
    ALREADY_IN_TARGET_STATE = "ALREADY_IN_TARGET_STATE"
    GUARDRAIL_BLOCKED = "GUARDRAIL_BLOCKED"


class EntityActionDeniedError(ApplicationError):
    def __init__(self, code: EntityActionDenialCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code


class UnknownEntityError(ApplicationError):
    """`entity_ref` no existe en `ad_entities` -- 404 en la presentacion,
    nunca 409: aqui no hay nada que negar, no hay entidad de la que hablar."""


@dataclass(frozen=True, slots=True)
class EntityActionResult:
    execution_id: ExecutionId
    outcome: ExecutionStatus
    undo_deadline: datetime | None


@dataclass(frozen=True, slots=True)
class PauseEntityCommand:
    entity_ref: EntityRef
    owner_email: str


@dataclass(frozen=True, slots=True)
class ResumeEntityCommand:
    entity_ref: EntityRef
    owner_email: str


@dataclass(frozen=True, slots=True)
class DeleteEntityCommand:
    entity_ref: EntityRef
    owner_email: str


@dataclass(frozen=True, slots=True, kw_only=True)
class EntityLifecycleDeps:
    """Colaboradores compartidos por `PauseEntity`/`ResumeEntity`/
    `DeleteEntity` (mismo criterio que `ApplyDefensiveAction`: freno,
    guardarrailes, cola de ejecucion y chokepoint son el MISMO camino de
    escritura de plan.md §6, cableados una vez por sesion en
    `Container.build_execution_use_cases`)."""

    ad_entities: AdEntityRepository
    brakes: BrakeStatePort
    proposals: ProposalRepository
    authorizations: AuthorizationRepository
    propose_action: ProposeAction
    guardrail_evaluator: GuardrailEvaluator
    guardrail_sets: GuardrailSetRepository
    spend_ledger: SpendLedger
    execution_queue: ExecutionQueuePort
    chokepoint: ExecutionChokepoint
    signer: SignerPort
    clock: Clock


class PauseEntity:
    """`entity.status == ACTIVE` -> `PAUSED`. Rechaza si el freno bloquea
    `human_approval` (a) o la entidad no es controlable / no esta activa (b)
    -- despues ejecuta por el MISMO camino que una propuesta aprobada (c)."""

    def __init__(self, deps: EntityLifecycleDeps) -> None:
        self._deps = deps

    async def execute(self, command: PauseEntityCommand) -> EntityActionResult:
        entity = await _require_entity(self._deps, command.entity_ref)
        await _require_brake_clear(self._deps, command.entity_ref)
        _require_pausable(entity)
        return await _authorize_and_run(
            self._deps,
            entity=entity,
            owner_email=command.owner_email,
            proposal_kind=ProposalKind.PAUSE,
            cause_type="owner_pause",
            cause_text="Pausado por el propietario desde el panel.",
            after_value=_PAUSED_VALUE,
        )


class ResumeEntity:
    """`entity.status == PAUSED` -> `ACTIVE`. Mismos controles que
    `PauseEntity`, en sentido contrario."""

    def __init__(self, deps: EntityLifecycleDeps) -> None:
        self._deps = deps

    async def execute(self, command: ResumeEntityCommand) -> EntityActionResult:
        entity = await _require_entity(self._deps, command.entity_ref)
        await _require_brake_clear(self._deps, command.entity_ref)
        _require_resumable(entity)
        return await _authorize_and_run(
            self._deps,
            entity=entity,
            owner_email=command.owner_email,
            proposal_kind=ProposalKind.RESUME,
            cause_type="owner_resume",
            cause_text="Reanudado por el propietario desde el panel.",
            after_value=_ACTIVE_VALUE,
        )


class DeleteEntity:
    """Borrado irreversible: sin `undo_deadline` nunca (`UndoGracePolicy.
    grace_for` devuelve `None` para `after=DELETED`) -- `POST
    /executions/{id}/undo` queda bloqueado por la misma guardia que ya usa
    `UndoExecution` para "intento sin ventana"."""

    def __init__(self, deps: EntityLifecycleDeps) -> None:
        self._deps = deps

    async def execute(self, command: DeleteEntityCommand) -> EntityActionResult:
        entity = await _require_entity(self._deps, command.entity_ref)
        await _require_brake_clear(self._deps, command.entity_ref)
        _require_deletable(entity)
        return await _authorize_and_run(
            self._deps,
            entity=entity,
            owner_email=command.owner_email,
            proposal_kind=ProposalKind.DELETE,
            cause_type="owner_delete",
            cause_text="Borrado por el propietario desde el panel.",
            after_value=_DELETED_VALUE,
        )


async def _require_entity(deps: EntityLifecycleDeps, entity_ref: EntityRef) -> AdEntity:
    entity = await deps.ad_entities.get_by_ref(entity_ref)
    if entity is None:
        raise UnknownEntityError(str(entity_ref))
    return entity


async def _require_brake_clear(deps: EntityLifecycleDeps, entity_ref: EntityRef) -> None:
    scope = brake_scope_from(GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref)))
    brake = await deps.brakes.get_effective(scope)
    if brake is not None and brake.blocks(AuthorizationKind.HUMAN_APPROVAL):
        raise EntityActionDeniedError(EntityActionDenialCode.BRAKE_ENGAGED, str(entity_ref))


def _require_pausable(entity: AdEntity) -> None:
    if entity.status is AdEntityStatus.PAUSED:
        raise EntityActionDeniedError(
            EntityActionDenialCode.ALREADY_IN_TARGET_STATE, str(entity.entity_ref)
        )
    if not entity.is_controllable or entity.status is not AdEntityStatus.ACTIVE:
        raise EntityActionDeniedError(
            EntityActionDenialCode.NOT_CONTROLLABLE, str(entity.entity_ref)
        )


def _require_resumable(entity: AdEntity) -> None:
    if entity.status is AdEntityStatus.ACTIVE:
        raise EntityActionDeniedError(
            EntityActionDenialCode.ALREADY_IN_TARGET_STATE, str(entity.entity_ref)
        )
    if not entity.is_controllable or entity.status is not AdEntityStatus.PAUSED:
        raise EntityActionDeniedError(
            EntityActionDenialCode.NOT_CONTROLLABLE, str(entity.entity_ref)
        )


def _require_deletable(entity: AdEntity) -> None:
    if entity.status is AdEntityStatus.REMOVED:
        raise EntityActionDeniedError(
            EntityActionDenialCode.ALREADY_IN_TARGET_STATE, str(entity.entity_ref)
        )
    if not entity.is_controllable:
        raise EntityActionDeniedError(
            EntityActionDenialCode.NOT_CONTROLLABLE, str(entity.entity_ref)
        )


async def _evaluate_guardrails(
    deps: EntityLifecycleDeps, scope: GuardrailScope, diff: ProposedDiff
) -> GuardrailVerdict:
    guardrails = await deps.guardrail_sets.get_effective(scope)
    ledger = await deps.spend_ledger.snapshot(scope, diff.entity_ref)
    before, after = money_pair_from_diff(diff)
    change = GuardrailChange(
        scope=scope,
        entity_ref=diff.entity_ref,
        authorization_kind=AuthorizationKind.HUMAN_APPROVAL,
        before=before,
        after=after,
    )
    return deps.guardrail_evaluator.evaluate(change, guardrails, ledger)


async def _authorize_and_run(
    deps: EntityLifecycleDeps,
    *,
    entity: AdEntity,
    owner_email: str,
    proposal_kind: ProposalKind,
    cause_type: str,
    cause_text: str,
    after_value: str,
) -> EntityActionResult:
    business_id: BusinessId = entity.business_id
    scope = GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity.entity_ref))
    diff = ProposedDiff.build(
        entity_ref=entity.entity_ref,
        parameter=_STATUS_PARAMETER,
        before=entity.status.value.upper(),
        after=after_value,
    )
    verdict = await _evaluate_guardrails(deps, scope, diff)
    if not verdict.allowed:
        raise EntityActionDeniedError(
            EntityActionDenialCode.GUARDRAIL_BLOCKED, ",".join(verdict.reasons)
        )

    propose_result = await deps.propose_action.execute(
        ProposeActionCommand(
            business_id=business_id,
            diff=diff,
            kind=proposal_kind,
            cause=Cause(text=cause_text, rule_id="owner"),
            cause_type=cause_type,
            evidence=(),
            estimated_impact=Money.zero(),
            urgency=Urgency.RECOMMENDED,
            expected_state_hash=entity.platform_state_hash.value,
        )
    )
    proposal = await deps.proposals.get(propose_result.proposal_id)
    assert proposal is not None  # noqa: S101 - se acaba de guardar en la misma transaccion

    now = deps.clock.now()
    authorization = sign_authorization(
        authorization_id=AuthorizationId.new(),
        proposal_id=proposal.proposal_id,
        kind=AuthorizationKind.HUMAN_APPROVAL,
        proposal_classification=proposal.classification,
        diff_hash=effective_diff(proposal.diff, verdict).diff_hash,
        guardrail_verdict_hash=verdict.verdict_hash,
        issued_by=owner_email,
        channel=AuthorizationChannel.PANEL,
        decided_at=now,
        expires_at=now + _AUTHORIZATION_TTL,
        signer=deps.signer,
        comment=cause_text,
    )
    await deps.authorizations.save(authorization)
    # Dos `save()`, no uno (mismo criterio que `SubmitApproval`/
    # `AuthorizeRuleAction`): el trigger de `proposals` solo permite
    # transiciones de UN salto (PENDING->APPROVED->SCHEDULED) -- un unico
    # UPSERT que saltase de PENDING a SCHEDULED lo rechaza.
    proposal.approve(proposal.diff.diff_hash, now)
    await deps.proposals.save(proposal)
    proposal.schedule_execution(0, now)
    await deps.proposals.save(proposal)
    await deps.execution_queue.save(
        ExecutionAttempt(
            execution_id=ExecutionId.new(),
            business_id=proposal.business_id,
            proposal_id=proposal.proposal_id,
            authorization_id=authorization.authorization_id,
            idempotency_key=build_idempotency_key(proposal.proposal_id, proposal.diff.diff_hash),
            platform_state_hash_before=entity.platform_state_hash.value,
        )
    )
    outcome = await deps.chokepoint.run_once(proposal_id=proposal.proposal_id)
    attempt = await deps.execution_queue.get_for_proposal(proposal.proposal_id)
    assert attempt is not None  # noqa: S101 - el chokepoint acaba de resolverlo
    return EntityActionResult(
        execution_id=attempt.execution_id,
        outcome=outcome or ExecutionStatus.FAILED,
        undo_deadline=attempt.undo_deadline,
    )
