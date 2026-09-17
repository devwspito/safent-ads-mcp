"""`ApplyDefensiveAction` (contracts/mcp-tools.md `apply_defensive_action`,
T072): los 6 controles, en la precedencia del contrato, sobre el mismo
`ExecutionChokepoint` que recorreria `RuleCycle`."""

from __future__ import annotations

import pytest

from safent_ads.accounts.domain.ad_entity import AdEntity, AdEntityStatus
from safent_ads.accounts.domain.budget import Budget, BudgetKind
from safent_ads.accounts.domain.money import Money as AccountsMoney
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.accounts.testing.in_memory_repositories import InMemoryAdEntityRepository
from safent_ads.execution.application.apply_defensive_action import (
    ApplyDefensiveAction,
    ApplyDefensiveActionCommand,
    ApplyDefensiveActionDeniedError,
)
from safent_ads.execution.application.authorize_rule_action import AuthorizeRuleAction
from safent_ads.execution.application.chokepoint import ExecutionChokepoint
from safent_ads.execution.application.platform_state_revalidator import PlatformStateRevalidator
from safent_ads.execution.domain.defensive_actions import (
    DefensiveActionDenialCode,
    DefensiveActionKind,
)
from safent_ads.execution.domain.execution_attempt import ExecutionAttempt, ExecutionStatus
from safent_ads.execution.domain.guardrails import (
    BrakeMode,
    BrakeScope,
    BrakeScopeKind,
    EmergencyBrake,
    GuardrailEvaluator,
    brake_scope_from,
)
from safent_ads.execution.domain.identifiers import ExecutionId
from safent_ads.execution.testing.fakes import (
    FakeAdsPlatformWritePort,
    FakeBrakeStatePort,
    FakeDecisionRecorder,
    FakeExecutionQueuePort,
    FakeExecutionReservations,
    FakeFreshnessPort,
    FakeGuardrailSetRepository,
    FakePlatformReaderPort,
    FakeRuleConditionPort,
    FakeSpendLedger,
    FakeUnitOfWork,
)
from safent_ads.proposals.application.propose_action import ProposeAction
from safent_ads.proposals.domain.authorization import AuthorizationVerifier
from safent_ads.proposals.domain.cause import Cause
from safent_ads.proposals.domain.classification import ClassificationPolicy
from safent_ads.proposals.domain.identifiers import AuthorizationId, ProposalId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import ExpiryPolicy
from safent_ads.proposals.testing.fakes import (
    FakeAuthorizationRepository,
    FakeProposalRepository,
    FakeSignerPort,
    FakeVerifierPort,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

from .conftest import NOW, empty_ledger, entity_scope, guardrail_set

_ENTITY_REF = EntityRef(PlatformCode.META, EntityLevel.CAMPAIGN, "c-defensive")
_SCOPE = entity_scope(_ENTITY_REF)
_STATE_HASH = PlatformStateHash.compute({"daily_budget_minor": 10_000})
_RULE_ID = "M05"


def _entity(
    *, status: AdEntityStatus = AdEntityStatus.ACTIVE, budget_minor: int = 10_000
) -> AdEntity:
    return AdEntity(
        business_id=BusinessId.new(),
        entity_ref=_ENTITY_REF,
        parent_ref=EntityRef(PlatformCode.META, EntityLevel.ACCOUNT, "act_1"),
        name="Campana defensiva",
        status=status,
        platform_state_hash=_STATE_HASH,
        is_controllable=True,
        budget=Budget(amount=AccountsMoney(budget_minor, "EUR"), kind=BudgetKind.DAILY),
    )


class _Scenario:
    def __init__(
        self,
        *,
        rule_is_live: bool = True,
        is_stale: bool = False,
        changes_today: int = 0,
        write_fail_with: Exception | None = None,
        entity: AdEntity | None = None,
    ) -> None:
        self.ad_entities = InMemoryAdEntityRepository([entity or _entity()])
        self.brakes = FakeBrakeStatePort()
        self.proposals = FakeProposalRepository()
        self.authorizations = FakeAuthorizationRepository()
        self.guardrail_sets = FakeGuardrailSetRepository({_SCOPE.ref: guardrail_set()})
        self.spend_ledger = FakeSpendLedger({_SCOPE.ref: empty_ledger(changes_today=changes_today)})
        self.guardrail_evaluator = GuardrailEvaluator()
        self.freshness = FakeFreshnessPort(lambda _ref: is_stale)
        self.rule_condition = FakeRuleConditionPort(lambda _rule_id, _ref: rule_is_live)
        self.queue = FakeExecutionQueuePort()
        self.recorder = FakeDecisionRecorder()
        self.platform_write = FakeAdsPlatformWritePort(fail_with=write_fail_with)
        self.clock = FixedClock(NOW)

        propose_action = ProposeAction(
            proposals=self.proposals,
            classification_policy=ClassificationPolicy(
                critical_impact_threshold=Money.of("100000")
            ),
            expiry_policy=ExpiryPolicy(),
            clock=self.clock,
        )
        authorize_rule_action = AuthorizeRuleAction(
            proposals=self.proposals,
            authorizations=self.authorizations,
            rule_condition=self.rule_condition,
            brakes=self.brakes,
            guardrail_evaluator=self.guardrail_evaluator,
            guardrail_sets=self.guardrail_sets,
            spend_ledger=self.spend_ledger,
            signer=FakeSignerPort(),
            clock=self.clock,
        )
        chokepoint = ExecutionChokepoint(
            reservations=FakeExecutionReservations(),
            queue=self.queue,
            uow=FakeUnitOfWork(),
            brakes=self.brakes,
            proposals=self.proposals,
            authorizations=self.authorizations,
            auth_verifier=AuthorizationVerifier(FakeVerifierPort(), self.clock),
            guardrail_evaluator=self.guardrail_evaluator,
            guardrail_sets=self.guardrail_sets,
            spend_ledger=self.spend_ledger,
            revalidator=PlatformStateRevalidator(
                FakePlatformReaderPort(state_hash_by_entity={str(_ENTITY_REF): _STATE_HASH.value})
            ),
            platform_write=self.platform_write,
            recorder=self.recorder,
            clock=self.clock,
        )
        self.use_case = ApplyDefensiveAction(
            ad_entities=self.ad_entities,
            brakes=self.brakes,
            freshness=self.freshness,
            rule_condition=self.rule_condition,
            guardrail_evaluator=self.guardrail_evaluator,
            guardrail_sets=self.guardrail_sets,
            spend_ledger=self.spend_ledger,
            proposals=self.proposals,
            propose_action=propose_action,
            authorize_rule_action=authorize_rule_action,
            execution_queue=self.queue,
            chokepoint=chokepoint,
            clock=self.clock,
        )

    async def engage_brake(self, mode: BrakeMode = BrakeMode.ALL) -> None:
        brake = EmergencyBrake(scope=brake_scope_from(_SCOPE), mode=mode)
        brake.engage("incidente", NOW)
        await self.brakes.save(brake)

    async def engage_scoped_brake(self, scope: BrakeScope, mode: BrakeMode = BrakeMode.ALL) -> None:
        brake = EmergencyBrake(scope=scope, mode=mode)
        brake.engage("incidente", NOW)
        await self.brakes.save(brake)


def _command(**overrides: object) -> ApplyDefensiveActionCommand:
    defaults: dict[str, object] = {
        "business_id": BusinessId.new(),
        "entity_ref": _ENTITY_REF,
        "rule_id": _RULE_ID,
        "action": DefensiveActionKind.LOWER_BUDGET,
        "cause": Cause(text="ROAS por debajo del objetivo en 7D", rule_id=_RULE_ID),
        "magnitude_pct": 0.30,
    }
    defaults.update(overrides)
    return ApplyDefensiveActionCommand(**defaults)  # type: ignore[arg-type]


class TestBrakeEngagedWinsFirst:
    async def test_brake_engaged_denies_before_anything_else(self) -> None:
        scenario = _Scenario(rule_is_live=False, is_stale=True)
        await scenario.engage_brake()

        with pytest.raises(ApplyDefensiveActionDeniedError) as excinfo:
            await scenario.use_case.execute(_command())

        assert excinfo.value.code is DefensiveActionDenialCode.BRAKE_ENGAGED

    async def test_global_brake_denies(self) -> None:
        """BUG corregido: solo se comprobaba el freno de la cuenta -- uno
        GLOBAL activo dejaba pasar la accion defensiva igualmente."""
        scenario = _Scenario()
        await scenario.engage_scoped_brake(BrakeScope(kind=BrakeScopeKind.GLOBAL))

        with pytest.raises(ApplyDefensiveActionDeniedError) as excinfo:
            await scenario.use_case.execute(_command())

        assert excinfo.value.code is DefensiveActionDenialCode.BRAKE_ENGAGED

    async def test_business_brake_denies(self) -> None:
        scenario = _Scenario()
        scenario.brakes.link_account_to_business(_SCOPE.ref, "negocio-1")
        business_scope = BrakeScope(kind=BrakeScopeKind.BUSINESS, ref="negocio-1")
        await scenario.engage_scoped_brake(business_scope)

        with pytest.raises(ApplyDefensiveActionDeniedError) as excinfo:
            await scenario.use_case.execute(_command())

        assert excinfo.value.code is DefensiveActionDenialCode.BRAKE_ENGAGED

    async def test_another_businesss_brake_does_not_deny(self) -> None:
        scenario = _Scenario()
        scenario.brakes.link_account_to_business(_SCOPE.ref, "negocio-1")
        other_business_scope = BrakeScope(kind=BrakeScopeKind.BUSINESS, ref="otro-negocio")
        await scenario.engage_scoped_brake(other_business_scope)

        result = await scenario.use_case.execute(_command())

        assert result.outcome is ExecutionStatus.EXECUTED


class TestStaleDataDeniesBeforeRuleApplicability:
    async def test_stale_data_denies(self) -> None:
        scenario = _Scenario(rule_is_live=False, is_stale=True)

        with pytest.raises(ApplyDefensiveActionDeniedError) as excinfo:
            await scenario.use_case.execute(_command())

        assert excinfo.value.code is DefensiveActionDenialCode.STALE_DATA


class TestRuleNotApplicableDenies:
    async def test_rule_not_live_denies(self) -> None:
        scenario = _Scenario(rule_is_live=False)

        with pytest.raises(ApplyDefensiveActionDeniedError) as excinfo:
            await scenario.use_case.execute(_command())

        assert excinfo.value.code is DefensiveActionDenialCode.RULE_NOT_APPLICABLE


class TestValidationErrorDenies:
    async def test_magnitude_above_catalog_ceiling_denies(self) -> None:
        scenario = _Scenario()

        with pytest.raises(ApplyDefensiveActionDeniedError) as excinfo:
            await scenario.use_case.execute(_command(magnitude_pct=0.90))

        assert excinfo.value.code is DefensiveActionDenialCode.VALIDATION_ERROR


class TestGuardrailBlockedDenies:
    async def test_max_changes_per_day_reached_denies(self) -> None:
        scenario = _Scenario(changes_today=2)

        with pytest.raises(ApplyDefensiveActionDeniedError) as excinfo:
            await scenario.use_case.execute(_command())

        assert excinfo.value.code is DefensiveActionDenialCode.GUARDRAIL_BLOCKED
        assert len(scenario.authorizations._by_id) == 0  # noqa: SLF001


class TestSucceedsThroughTheRealChokepoint:
    async def test_lower_budget_runs_end_to_end_and_records_a_decision(self) -> None:
        scenario = _Scenario()

        result = await scenario.use_case.execute(_command())

        assert result.outcome is ExecutionStatus.EXECUTED
        assert scenario.platform_write.call_count == 1
        assert len(scenario.recorder.recorded) >= 1

    async def test_pause_transitions_status_to_paused(self) -> None:
        scenario = _Scenario()

        result = await scenario.use_case.execute(
            _command(action=DefensiveActionKind.PAUSE, magnitude_pct=None)
        )

        assert result.outcome is ExecutionStatus.EXECUTED


class TestClaimsOnlyItsOwnAttemptUnderBacklog:
    async def test_does_not_execute_another_business_pending_row(self) -> None:
        """Security review F2/F3, C-2 nit 3: `apply_defensive_action.py:331`
        llamaba a `run_once()` sin argumentos -- bajo backlog, `claim_next`
        (`sql_execution_queue.py:95`) se lleva la fila reclamable MAS
        ANTIGUA sin filtrar por propuesta/negocio, asi que la llamada del
        agente ejecutaba el intento de OTRO negocio y reportaba ese
        desenlace como si fuera el propio. Se siembra un intento pendiente
        de otro negocio, mas antiguo (encolado antes), y se comprueba que
        `execute()` ni lo toca ni reporta su desenlace."""
        scenario = _Scenario()
        # Construido igual que `apply_defensive_action.py:319-330` construye
        # el suyo (NO `ExecutionAttempt.claim()`, que ya trae `started_at`
        # puesto y por tanto no reclamable): `status=CLAIMED`,
        # `started_at=None`, exactamente la fila que un `claim_next()` sin
        # filtrar encontraria "mas antigua".
        foreign_attempt = ExecutionAttempt(
            execution_id=ExecutionId.new(),
            business_id=BusinessId.new(),
            proposal_id=ProposalId.new(),
            authorization_id=AuthorizationId.new(),
            idempotency_key="exec-foreign-attempt",
        )
        await scenario.queue.save(foreign_attempt)

        result = await scenario.use_case.execute(_command())

        assert result.outcome is ExecutionStatus.EXECUTED
        assert scenario.platform_write.call_count == 1

        still_pending = await scenario.queue.get_for_proposal(foreign_attempt.proposal_id)
        assert still_pending is not None
        assert still_pending.status is ExecutionStatus.CLAIMED
        assert still_pending.started_at is None
