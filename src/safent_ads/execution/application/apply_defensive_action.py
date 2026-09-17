"""`ApplyDefensiveAction` (contracts/mcp-tools.md `apply_defensive_action`):
la unica via del agente hacia el chokepoint (T072, C-2).

Los 6 controles del contrato, en el orden que fija
`execution.domain.defensive_actions.precheck_apply_defensive_action`
(freno > datos obsoletos > regla no aplicable > accion no defensiva >
guardarraíl): calcula las cuatro banderas y el veredicto de guardarrailes
ANTES de intentar nada, con los MISMOS puertos que usa
`AuthorizeRuleAction` -- pero por separado, porque `apply_defensive_action`
necesita distinguir `STALE_DATA`/`BRAKE_ENGAGED`/`VALIDATION_ERROR` con
precedencia propia (contrato), mientras que `AuthorizeRuleAction` colapsa
la primera mitad de esos casos en `RULE_NOT_APPLICABLE` (su unico llamador
hasta ahora no necesitaba distinguirlos).

Solo si el precheck deja pasar la accion se llama a `AuthorizeRuleAction`
(paso 5 del contrato: acuñar la `rule_authorization`) y despues al
`ExecutionChokepoint` (paso 6) -- MISMO camino que recorreria
`RuleCycle`/`ExecutionCycle` en su siguiente vuelta, solo que aqui se
ejecuta ya, sincronamente, porque el agente pidio una accion concreta y
espera su desenlace en la misma llamada (Assumption documentada: el
contrato no fija la cadencia de `apply_defensive_action`, y devolver
`execution_id, outcome, applied_value, undo_deadline` en la respuesta solo
tiene sentido si el intento ya se resolvio)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from safent_ads.accounts.application.ports import AdEntityRepository
from safent_ads.accounts.domain.ad_entity import AdEntity, AdEntityStatus
from safent_ads.accounts.domain.money import Money as AccountsMoney
from safent_ads.execution.application.authorize_rule_action import (
    AuthorizeRuleAction,
    AuthorizeRuleActionCommand,
)
from safent_ads.execution.application.chokepoint import ExecutionChokepoint
from safent_ads.execution.application.ports import (
    BrakeStatePort,
    ExecutionQueuePort,
    FreshnessPort,
    GuardrailSetRepository,
    RuleConditionPort,
)
from safent_ads.execution.domain.defensive_actions import (
    DefensiveAction,
    DefensiveActionDenialCode,
    DefensiveActionKind,
    precheck_apply_defensive_action,
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
)
from safent_ads.execution.domain.identifiers import ExecutionId
from safent_ads.proposals.application.ports import ProposalRepository
from safent_ads.proposals.application.propose_action import ProposeAction, ProposeActionCommand
from safent_ads.proposals.domain.authorization import AuthorizationKind
from safent_ads.proposals.domain.cause import Cause
from safent_ads.proposals.domain.classification import ProposalKind
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Urgency
from safent_ads.proposals.domain.proposal import ProposedDiff
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import ApplicationError
from safent_ads.shared.ids import BusinessId, EntityRef

__all__ = [
    "ApplyDefensiveAction",
    "ApplyDefensiveActionCommand",
    "ApplyDefensiveActionDeniedError",
    "ApplyDefensiveActionResult",
    "UnsupportedDefensiveActionError",
]

_MINOR_UNITS_PER_MAJOR = Decimal(100)
_BUDGET_PARAMETER = "daily_budget"
_STATUS_PARAMETER = "status"

# `contracts/mcp-tools.md::apply_defensive_action.action` solo admite estos
# cuatro (`add_negative_keyword`/`rotate_out_creative` no tienen todavia un
# concepto de "valor antes/despues" en `accounts.domain` -- ni palabra clave
# negativa ni rotacion de creatividad se modelan hoy como un campo de
# `AdEntity`. Denegar con un tipo propio en vez de forzar uno de los 5
# codigos del contrato, que describen otra cosa (ver docstring del modulo).
_SUPPORTED_ACTIONS = frozenset({DefensiveActionKind.LOWER_BUDGET, DefensiveActionKind.PAUSE})

_ACTION_TO_KIND = {
    DefensiveActionKind.LOWER_BUDGET: ProposalKind.BUDGET_DECREASE,
    DefensiveActionKind.PAUSE: ProposalKind.PAUSE,
}


class UnsupportedDefensiveActionError(ApplicationError):
    """`action` reconocida por el contrato pero sin traduccion a
    antes/despues todavia (ver `_SUPPORTED_ACTIONS`). Documentado, no
    fabricado: `backend-engineer` la completa cuando `accounts.domain`
    modele palabras clave negativas / rotacion de creatividad."""


class ApplyDefensiveActionDeniedError(ApplicationError):
    def __init__(self, code: DefensiveActionDenialCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code


@dataclass(frozen=True, slots=True)
class ApplyDefensiveActionCommand:
    business_id: BusinessId
    entity_ref: EntityRef
    rule_id: str
    action: DefensiveActionKind
    cause: Cause
    magnitude_pct: float | None = None


@dataclass(frozen=True, slots=True)
class ApplyDefensiveActionResult:
    execution_id: ExecutionId
    outcome: ExecutionStatus
    applied_value: object
    undo_deadline: datetime | None


class ApplyDefensiveAction:
    def __init__(
        self,
        *,
        ad_entities: AdEntityRepository,
        brakes: BrakeStatePort,
        freshness: FreshnessPort,
        rule_condition: RuleConditionPort,
        guardrail_evaluator: GuardrailEvaluator,
        guardrail_sets: GuardrailSetRepository,
        spend_ledger: SpendLedger,
        proposals: ProposalRepository,
        propose_action: ProposeAction,
        authorize_rule_action: AuthorizeRuleAction,
        execution_queue: ExecutionQueuePort,
        chokepoint: ExecutionChokepoint,
        clock: Clock,
    ) -> None:
        self._ad_entities = ad_entities
        self._brakes = brakes
        self._freshness = freshness
        self._rule_condition = rule_condition
        self._guardrail_evaluator = guardrail_evaluator
        self._guardrail_sets = guardrail_sets
        self._spend_ledger = spend_ledger
        self._proposals = proposals
        self._propose_action = propose_action
        self._authorize_rule_action = authorize_rule_action
        self._execution_queue = execution_queue
        self._chokepoint = chokepoint
        self._clock = clock

    async def execute(self, command: ApplyDefensiveActionCommand) -> ApplyDefensiveActionResult:
        if command.action not in _SUPPORTED_ACTIONS:
            raise UnsupportedDefensiveActionError(
                f"{command.action.value} aun no tiene traduccion antes/despues"
            )
        entity = await self._ad_entities.get_by_ref(command.entity_ref)
        if entity is None:
            raise ApplyDefensiveActionDeniedError(
                DefensiveActionDenialCode.VALIDATION_ERROR, "entidad desconocida"
            )
        scope = GuardrailScope(kind=ScopeKind.ENTITY, ref=str(command.entity_ref))
        # Nota de precedencia (gap conocido, no arreglado aqui por alcance):
        # una entidad SIN presupuesto deniega VALIDATION_ERROR aqui, ANTES
        # de comprobar freno/frescura/regla -- si esa misma entidad tambien
        # tuviera datos obsoletos, el contrato pediria STALE_DATA primero.
        # Solo importa cuando la entidad esta rota (sin presupuesto
        # sincronizado); documentado para revision de security-engineer.
        before, after = self._resolve_before_after(command, entity)
        action = DefensiveAction(kind=command.action, magnitude_pct=command.magnitude_pct)

        brake_engaged = await self._brake_engaged(scope)
        data_is_stale = await self._freshness.is_stale(command.entity_ref)
        rule_condition_is_live = await self._rule_condition.is_condition_live(
            command.rule_id, command.entity_ref
        )
        guardrail_verdict = await self._maybe_guardrail_verdict(
            brake_engaged=brake_engaged,
            data_is_stale=data_is_stale,
            rule_condition_is_live=rule_condition_is_live,
            action=action,
            before=before,
            after=after,
            scope=scope,
            entity_ref=command.entity_ref,
        )

        denial = precheck_apply_defensive_action(
            action,
            before,
            after,
            brake_engaged=brake_engaged,
            rule_condition_is_live=rule_condition_is_live,
            data_is_stale=data_is_stale,
            guardrail_verdict=guardrail_verdict,
        )
        if denial is not None:
            raise ApplyDefensiveActionDeniedError(denial, str(command.entity_ref))

        return await self._propose_authorize_and_run(command, entity, before, after)

    async def _brake_engaged(self, scope: GuardrailScope) -> bool:
        brake = await self._brakes.get_effective(brake_scope_from(scope))
        return brake is not None and brake.blocks(AuthorizationKind.RULE_AUTHORIZATION)

    async def _maybe_guardrail_verdict(
        self,
        *,
        brake_engaged: bool,
        data_is_stale: bool,
        rule_condition_is_live: bool,
        action: DefensiveAction,
        before: Money,
        after: Money,
        scope: GuardrailScope,
        entity_ref: EntityRef,
    ) -> GuardrailVerdict | None:
        # No merece la pena leer guardarrailes si ya se sabe que se va a
        # denegar por algo anterior en la precedencia (freno, datos
        # obsoletos, regla, validacion): un guardarraíl inexistente para
        # esa entidad no debe tapar el motivo real de la denegacion.
        if brake_engaged or data_is_stale or not rule_condition_is_live:
            return None
        if not action.is_defensive(before, after) or not action.respects_catalog_ceiling():
            return None
        guardrails = await self._guardrail_sets.get_effective(scope)
        ledger = await self._spend_ledger.snapshot(scope, entity_ref)
        change = GuardrailChange(
            scope=scope,
            entity_ref=entity_ref,
            authorization_kind=AuthorizationKind.RULE_AUTHORIZATION,
            before=before,
            after=after,
        )
        return self._guardrail_evaluator.evaluate(change, guardrails, ledger)

    def _resolve_before_after(
        self, command: ApplyDefensiveActionCommand, entity: AdEntity
    ) -> tuple[Money, Money]:
        if command.action is DefensiveActionKind.LOWER_BUDGET:
            return self._lower_budget_values(entity, command.magnitude_pct)
        return self._pause_values(entity)

    def _lower_budget_values(
        self, entity: AdEntity, magnitude_pct: float | None
    ) -> tuple[Money, Money]:
        budget = entity.budget
        if budget is None:
            raise ApplyDefensiveActionDeniedError(
                DefensiveActionDenialCode.VALIDATION_ERROR, "la entidad no tiene presupuesto"
            )
        before = _to_proposals_money(budget.amount)
        pct = Decimal(str(magnitude_pct)) if magnitude_pct is not None else Decimal("0")
        after = before.scaled_by(Decimal("1") - pct)
        return before, after

    def _pause_values(self, entity: AdEntity) -> tuple[Money, Money]:
        # `money_pair_from_diff` (guardrails.py) ya trata cualquier diff no
        # monetario como `Money.zero()` para el evaluador -- una pausa se
        # modela igual aqui: after=0 equivalente, before=0 (ya pausada) o el
        # presupuesto vivo si estaba activa, mismo criterio documentado en
        # `GuardrailChange`.
        budget = entity.budget
        before = _to_proposals_money(budget.amount) if budget is not None else Money.zero()
        after = Money.zero(before.currency) if entity.status is AdEntityStatus.ACTIVE else before
        return before, after

    async def _propose_authorize_and_run(
        self,
        command: ApplyDefensiveActionCommand,
        entity: AdEntity,
        before: Money,
        after: Money,
    ) -> ApplyDefensiveActionResult:
        parameter = (
            _BUDGET_PARAMETER if command.action is DefensiveActionKind.LOWER_BUDGET
            else _STATUS_PARAMETER
        )
        diff = ProposedDiff.build(
            entity_ref=command.entity_ref, parameter=parameter, before=before, after=after
        )
        propose_result = await self._propose_action.execute(
            ProposeActionCommand(
                business_id=command.business_id,
                diff=diff,
                kind=_ACTION_TO_KIND[command.action],
                cause=command.cause,
                cause_type=f"defensive:{command.action.value}",
                evidence=(),
                estimated_impact=(before - after) if before > after else (after - before),
                urgency=Urgency.RECOMMENDED,
                expected_state_hash=entity.platform_state_hash.value,
            )
        )
        authorization = await self._authorize_rule_action.execute(
            AuthorizeRuleActionCommand(propose_result.proposal_id, command.rule_id)
        )
        proposal = await self._proposals.get(propose_result.proposal_id)
        assert proposal is not None  # noqa: S101 - se acaba de guardar en la misma transaccion
        now = self._clock.now()
        proposal.schedule_execution(0, now)
        await self._proposals.save(proposal)
        await self._execution_queue.save(
            ExecutionAttempt(
                execution_id=ExecutionId.new(),
                business_id=proposal.business_id,
                proposal_id=proposal.proposal_id,
                authorization_id=authorization.authorization_id,
                idempotency_key=build_idempotency_key(
                    proposal.proposal_id, proposal.diff.diff_hash
                ),
                platform_state_hash_before=entity.platform_state_hash.value,
            )
        )
        # C-2 nit 3 (security review F2/F3): reclama SOLO el intento que se
        # acaba de encolar dos lineas arriba -- sin el filtro, bajo carga
        # `run_once()` sin argumentos se lleva la fila reclamable mas
        # antigua de CUALQUIER negocio y este metodo reportaba su desenlace
        # como si fuera el propio.
        outcome = await self._chokepoint.run_once(proposal_id=proposal.proposal_id)
        attempt = await self._execution_queue.get_for_proposal(proposal.proposal_id)
        assert attempt is not None  # noqa: S101 - el chokepoint acaba de resolverlo
        return ApplyDefensiveActionResult(
            execution_id=attempt.execution_id,
            outcome=outcome or ExecutionStatus.FAILED,
            applied_value=attempt.applied_value,
            undo_deadline=attempt.undo_deadline,
        )


def _to_proposals_money(accounts_money: AccountsMoney) -> Money:
    amount = Decimal(accounts_money.minor_units) / _MINOR_UNITS_PER_MAJOR
    return Money.of(amount, accounts_money.currency)
