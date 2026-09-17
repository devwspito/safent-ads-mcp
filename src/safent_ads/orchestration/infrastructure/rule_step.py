"""`LiveRuleStep`: adaptador real de `RuleEvaluationStepPort` (plan.md §7
`RuleCycle`, tasks.md T068). Por cada entidad controlable de un negocio,
relee la ultima `Signal` y pregunta a `rules.domain.rule_evaluator.
evaluate_rule` que autonomia le corresponde (el mismo evaluador que ya usa
`SqlRuleConditionPort` para `apply_defensive_action` -- una sola verdad):

- `RuleOutcome.NOOP`/`NOTIFY`: nada que hacer aqui (el ticker ya cubre
  `NOTIFY` via `NotificationCycle`, que lee `signals` directamente).
- `RuleOutcome.PROPOSE` (autonomia `APPROVAL`): crea/actualiza una
  propuesta `pendiente` (`ProposeAction`, FR-20) sujeta al presupuesto de
  atencion (<=10 propuestas vivas por negocio, FR-19 -- Assumption
  documentada: el contrato no fija si es por dia natural o "cola visible",
  se usa "cola visible" porque es lo que protege SC-3).
- `RuleOutcome.AUTO_ACTION`: en produccion crea una propuesta pendiente,
  igual que PROPOSE. El propietario siempre debe aprobar desde el panel.
  Solo el evaluador reutilizable con require_owner_approval=False permite
  la estrategia de reglas restringida descrita a continuacion; la composicion
  de producto nunca la activa: crea la propuesta, la
  autoriza (`AuthorizeRuleAction` -- re-verifica condicion/freno/
  guardarraíl en vivo, nunca se fia de este paso) y encola su ejecucion
  (`ExecutionCycle` la recoge en su siguiente vuelta de 30 s).

T158 (profitability-engine.md §5 nodo 1): antes de evaluar cada entidad se
resuelve `measurement_frozen` UNA VEZ por cuenta (`SqlMeasurementFreezeGate`,
`unattributed_share`/`delta_hat` reales de T156). Con la cuenta congelada,
ninguna regla `BUY` (ni `PROPOSE` ni `AUTO_ACTION`) sigue adelante --
`SELL`/`EXIT` (pause/lower) no se tocan: son las senales defensivas que el
nodo 1 nunca debe bloquear."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.accounts.domain.ad_entity import AdEntity, AdEntityStatus
from safent_ads.accounts.domain.money import Money as AccountsMoney
from safent_ads.accounts.infrastructure.sql_repositories import (
    SqlAccountRepository,
    SqlAdEntityRepository,
)
from safent_ads.execution.application.authorize_rule_action import (
    AuthorizeRuleAction,
    AuthorizeRuleActionCommand,
    RuleAuthorizationDeniedError,
)
from safent_ads.execution.application.chokepoint import ExecutionChokepoint
from safent_ads.execution.application.ports import ExecutionQueuePort
from safent_ads.execution.domain.execution_attempt import ExecutionAttempt, build_idempotency_key
from safent_ads.execution.domain.identifiers import ExecutionId
from safent_ads.orchestration.infrastructure.measurement_freeze_gate import (
    SqlMeasurementFreezeGate,
)
from safent_ads.proposals.application.ports import ProposalRepository
from safent_ads.proposals.application.propose_action import ProposeAction, ProposeActionCommand
from safent_ads.proposals.domain.cause import Cause
from safent_ads.proposals.domain.classification import ProposalKind
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Urgency
from safent_ads.proposals.domain.proposal import ProposedDiff
from safent_ads.rules.domain.autonomy import ActionKind
from safent_ads.rules.domain.rule import Rule
from safent_ads.rules.domain.rule_evaluator import RuleOutcome, evaluate_rule
from safent_ads.rules.domain.stored_rule import StoredRule
from safent_ads.rules.infrastructure.sql_repositories import SqlRuleRepository
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId, EntityLevel
from safent_ads.signals.infrastructure.sql_repositories import SqlSignalRepository

logger = structlog.get_logger(__name__)

_BUDGET_PARAMETER = "daily_budget"
_STATUS_PARAMETER = "status"
_MINOR_UNITS_PER_MAJOR = Decimal(100)
_PERCENT_TO_FRACTION = Decimal(100)
_ATTENTION_BUDGET_PER_BUSINESS = 10
# Grace corta y no nula (nunca `grace=0` para lo que decide el motor de
# reglas tampoco, por higiene): T083 fija 20 s/45 s para propuestas
# aprobadas por humano; una accion `AUTO` ya decidida no necesita ventana
# de reconsideracion humana, pero SI necesita que el reclamo de la cola
# (`scheduled_at <= now`) no dependa de que los relojes vayan exactamente
# sincronizados entre el momento de agendar y el siguiente tick del
# `ExecutionCycle` (30 s). Assumption documentada, pendiente de que
# `backend-engineer` la revise junto con T083.
_AUTO_ACTION_GRACE_SECONDS = 5

# Solo las acciones con traduccion mecanica antes/despues (mismo catalogo
# que `execution.application.apply_defensive_action`, ver su docstring
# sobre por que las demas quedan fuera). `BUY` se anade aqui porque
# `RuleOutcome.PROPOSE` (autonomia `APPROVAL`) SI puede subir gasto -- el
# invariante "AUTO nunca sube gasto" ya lo protege `Rule.__post_init__`.
_ACTION_TO_PARAMETER = {
    ActionKind.SELL: _BUDGET_PARAMETER,
    ActionKind.BUY: _BUDGET_PARAMETER,
    ActionKind.EXIT: _STATUS_PARAMETER,
}
_ACTION_TO_PROPOSAL_KIND = {
    ActionKind.SELL: ProposalKind.BUDGET_DECREASE,
    ActionKind.BUY: ProposalKind.BUDGET_INCREASE,
    ActionKind.EXIT: ProposalKind.PAUSE,
}


class WorkerExecutionUseCases(Protocol):
    """Lo que `LiveRuleStep`/`ExecutionCycle` necesitan de `Container.
    build_execution_use_cases` -- Protocol propio en vez de importar
    `composition.container.ExecutionUseCases` (orchestration no importa la
    raiz de composicion, plan.md §4); satisfecho estructuralmente sin
    herencia, igual que `SqlBrakeStatePort` satisface `BrakeStatePort`.

    Miembros como `@property` (de solo lectura) a proposito: un campo de
    instancia declarado como atributo simple en un `Protocol` exige tipo
    INVARIANTE (mypy no puede probar que nadie le va a asignar un valor
    menos especifico), así que `ExecutionUseCases.proposals:
    SqlProposalRepository` no lo satisfaría contra `proposals:
    ProposalRepository` aunque `SqlProposalRepository` sea-un
    `ProposalRepository`. Con `@property` mypy sabe que es de solo
    lectura y acepta el tipo mas especifico (covarianza)."""

    @property
    def propose_action(self) -> ProposeAction: ...

    @property
    def authorize_rule_action(self) -> AuthorizeRuleAction: ...

    @property
    def execution_queue(self) -> ExecutionQueuePort: ...

    @property
    def proposals(self) -> ProposalRepository: ...

    @property
    def chokepoint(self) -> ExecutionChokepoint: ...


# Alias `Callable`, no un `Protocol` con `__call__`: mypy no aplica
# correctamente la covarianza del tipo de retorno cuando el valor pasado es
# un `Callable[[X], TipoConcreto]` contra un `Protocol` de una sola
# `__call__` cuyo retorno es OTRO `Protocol` (doble indireccion) -- probado
# en aislado, un alias `Callable[...]` normal si la resuelve bien.
ExecutionUseCasesFactory = Callable[[AsyncSession], WorkerExecutionUseCases]


class LiveRuleStep:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        build_use_cases: ExecutionUseCasesFactory,
        clock: Clock,
        *,
        require_owner_approval: bool = True,
    ) -> None:
        self._session_factory = session_factory
        self._build_use_cases = build_use_cases
        self._clock = clock
        self._require_owner_approval = require_owner_approval

    async def run(self, business_id: BusinessId, cycle_id: str, now: datetime) -> None:
        del now
        async with self._session_factory() as session:
            use_cases = self._build_use_cases(session)
            rules = SqlRuleRepository(session)
            signals = SqlSignalRepository(session, cycle_id=_cycle_uuid(cycle_id))
            accounts = await SqlAccountRepository(session).list_for_observation(business_id)
            entity_repo = SqlAdEntityRepository(session)
            freeze_gate = SqlMeasurementFreezeGate(session, self._clock)
            pending_budget_used = 0
            for account in accounts:
                measurement_frozen = await freeze_gate.is_frozen(business_id, account)
                campaign_entities = [
                    entity
                    for entity in await entity_repo.list_by_account(account.account_ref)
                    if entity.entity_ref.level == EntityLevel.CAMPAIGN and entity.is_controllable
                ]
                for entity in campaign_entities:
                    pending_budget_used = await self._evaluate_entity(
                        business_id=business_id,
                        entity=entity,
                        rules=rules,
                        signals=signals,
                        use_cases=use_cases,
                        pending_budget_used=pending_budget_used,
                        measurement_frozen=measurement_frozen,
                    )
            await session.commit()

    async def _evaluate_entity(
        self,
        *,
        business_id: BusinessId,
        entity: AdEntity,
        rules: SqlRuleRepository,
        signals: SqlSignalRepository,
        use_cases: WorkerExecutionUseCases,
        pending_budget_used: int,
        measurement_frozen: bool,
    ) -> int:
        firing = await _resolve_firing(entity, rules, signals)
        if firing is None:
            return pending_budget_used
        stored, outcome, diff, cause = firing

        if measurement_frozen and stored.rule.action_kind is ActionKind.BUY:
            # T158, profitability-engine.md §5 nodo 1: "Congelar BUY".
            # Defensivo: SELL/EXIT (pause/lower) no se ven afectadas por
            # esta guarda -- solo la subida de gasto se congela.
            logger.info(
                "rule_cycle_buy_frozen_measurement_broken",
                business_id=str(business_id),
                entity_ref=str(entity.entity_ref),
                rule_code=stored.code,
            )
            return pending_budget_used

        if outcome is RuleOutcome.AUTO_ACTION and not self._require_owner_approval:
            await self._propose_and_authorize(
                business_id=business_id,
                diff=diff,
                entity=entity,
                stored=stored,
                cause=cause,
                use_cases=use_cases,
            )
            return pending_budget_used

        # `RuleOutcome.PROPOSE`: cuenta contra el presupuesto de atencion.
        if pending_budget_used >= _ATTENTION_BUDGET_PER_BUSINESS:
            logger.info(
                "rule_cycle_deferred_attention_budget",
                business_id=str(business_id),
                entity_ref=str(entity.entity_ref),
                rule_code=stored.code,
            )
            return pending_budget_used
        await use_cases.propose_action.execute(
            ProposeActionCommand(
                business_id=business_id,
                diff=diff,
                kind=_ACTION_TO_PROPOSAL_KIND[stored.rule.action_kind],
                cause=cause,
                cause_type=f"rule:{stored.code}",
                evidence=(),
                estimated_impact=_impact(diff),
                urgency=Urgency.RECOMMENDED,
                expected_state_hash=entity.platform_state_hash.value,
            )
        )
        return pending_budget_used + 1

    async def _propose_and_authorize(
        self,
        *,
        business_id: BusinessId,
        diff: ProposedDiff,
        entity: AdEntity,
        stored: StoredRule,
        cause: Cause,
        use_cases: WorkerExecutionUseCases,
    ) -> None:
        propose_result = await use_cases.propose_action.execute(
            ProposeActionCommand(
                business_id=business_id,
                diff=diff,
                kind=_ACTION_TO_PROPOSAL_KIND[stored.rule.action_kind],
                cause=cause,
                cause_type=f"rule:{stored.code}",
                evidence=(),
                estimated_impact=_impact(diff),
                urgency=Urgency.RECOMMENDED,
                expected_state_hash=entity.platform_state_hash.value,
            )
        )
        try:
            authorization = await use_cases.authorize_rule_action.execute(
                AuthorizeRuleActionCommand(propose_result.proposal_id, stored.code)
            )
        except RuleAuthorizationDeniedError as exc:
            # Reevaluado en vivo dentro de `AuthorizeRuleAction` (freno,
            # guardarraíl): denegar aqui es correcto, no un fallo del
            # ciclo -- la propuesta se queda `pendiente` para revision
            # humana en vez de perderse.
            logger.info(
                "rule_cycle_auto_authorization_denied",
                proposal_id=str(propose_result.proposal_id),
                reason=exc.reason.value,
            )
            return

        now = self._clock.now()
        proposal = await use_cases.proposals.get(propose_result.proposal_id)
        if proposal is None:
            return
        proposal.schedule_execution(_AUTO_ACTION_GRACE_SECONDS, now)
        await use_cases.proposals.save(proposal)
        await use_cases.execution_queue.save(
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


async def _resolve_firing(
    entity: AdEntity, rules: SqlRuleRepository, signals: SqlSignalRepository
) -> tuple[StoredRule, RuleOutcome, ProposedDiff, Cause] | None:
    """`None` cubre cinco motivos de "nada que hacer aqui" a la vez (sin
    senal, regla apagada/desconocida, `NOOP`/`NOTIFY`, accion sin
    traduccion mecanica): todos denegar-por-defecto, ninguno un error."""
    signal = await signals.find_latest_for_entity(entity_ref=entity.entity_ref)
    if signal is None:
        return None
    stored = await rules.get_by_code(signal.rule_code)
    if stored is None or not stored.is_enabled:
        return None
    outcome = evaluate_rule(stored.rule, signal)
    if outcome is RuleOutcome.NOOP or outcome is RuleOutcome.NOTIFY:
        return None
    diff = _build_diff(stored.rule, entity)
    if diff is None:
        logger.info(
            "rule_cycle_action_not_wired",
            rule_code=stored.code,
            action=stored.rule.action_kind.value,
            entity_ref=str(entity.entity_ref),
        )
        return None
    cause = Cause(text=signal.cause_sentence[:140], signal_id=signal.signal_id, rule_id=stored.code)
    return stored, outcome, diff, cause


def _build_diff(rule: Rule, entity: AdEntity) -> ProposedDiff | None:
    parameter = _ACTION_TO_PARAMETER.get(rule.action_kind)
    if parameter is None or rule.magnitude_pct is None:
        return None
    if parameter == _BUDGET_PARAMETER:
        return _budget_diff(rule, entity)
    return _status_diff(entity)


def _budget_diff(rule: Rule, entity: AdEntity) -> ProposedDiff | None:
    if entity.budget is None:
        return None
    before = _to_money(entity.budget.amount)
    # `rules.magnitude_pct` es 0-100 (CHECK de `0007_rules_guardrails`,
    # mismo convenio que `rules.yaml`: "magnitude_pct: 30" = 30 %) --
    # `Money.scaled_by`/`GuardrailSet.max_step_pct` esperan una fraccion
    # 0-1. Sin este /100 una regla al 30 % restaba 29 veces el presupuesto
    # en vez de un 30 %.
    pct = Decimal(str(rule.magnitude_pct)) / _PERCENT_TO_FRACTION
    factor = Decimal("1") - pct if rule.action_kind is ActionKind.SELL else Decimal("1") + pct
    after = before.scaled_by(factor)
    return ProposedDiff.build(
        entity_ref=entity.entity_ref, parameter=_BUDGET_PARAMETER, before=before, after=after
    )


def _status_diff(entity: AdEntity) -> ProposedDiff | None:
    currency = entity.budget.amount.currency if entity.budget is not None else "EUR"
    before = _to_money(entity.budget.amount) if entity.budget is not None else Money.zero(currency)
    after = Money.zero(currency) if entity.status is AdEntityStatus.ACTIVE else before
    return ProposedDiff.build(
        entity_ref=entity.entity_ref, parameter=_STATUS_PARAMETER, before=before, after=after
    )


def _to_money(accounts_money: AccountsMoney) -> Money:
    amount = Decimal(accounts_money.minor_units) / _MINOR_UNITS_PER_MAJOR
    return Money.of(amount, accounts_money.currency)


def _impact(diff: ProposedDiff) -> Money:
    before = diff.before if isinstance(diff.before, Money) else Money.zero()
    after = diff.after if isinstance(diff.after, Money) else Money.zero()
    return (before - after) if before > after else (after - before)


def _cycle_uuid(cycle_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(cycle_id)
    except ValueError:
        return uuid.uuid5(uuid.NAMESPACE_URL, cycle_id)
