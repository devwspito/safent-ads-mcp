"""`PauseEntity`/`ResumeEntity`/`DeleteEntity`: freno y controlabilidad
deniegan ANTES de tocar nada; el camino feliz corre por el MISMO
`ExecutionChokepoint` que una propuesta aprobada (mismo criterio que
`test_apply_defensive_action.py`)."""

from __future__ import annotations

import pytest

from safent_ads.accounts.domain.ad_entity import AdEntity, AdEntityStatus
from safent_ads.accounts.domain.budget import Budget, BudgetKind
from safent_ads.accounts.domain.money import Money as AccountsMoney
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.accounts.testing.in_memory_repositories import InMemoryAdEntityRepository
from safent_ads.execution.application.chokepoint import ExecutionChokepoint
from safent_ads.execution.application.entity_lifecycle_actions import (
    DeleteEntity,
    DeleteEntityCommand,
    EntityActionDenialCode,
    EntityActionDeniedError,
    EntityLifecycleDeps,
    PauseEntity,
    PauseEntityCommand,
    ResumeEntity,
    ResumeEntityCommand,
    UnknownEntityError,
)
from safent_ads.execution.application.platform_state_revalidator import PlatformStateRevalidator
from safent_ads.execution.domain.execution_attempt import ExecutionStatus
from safent_ads.execution.domain.guardrails import (
    BrakeMode,
    BrakeScope,
    BrakeScopeKind,
    EmergencyBrake,
    GuardrailEvaluator,
    brake_scope_from,
)
from safent_ads.execution.testing.fakes import (
    FakeAdsPlatformWritePort,
    FakeBrakeStatePort,
    FakeDecisionRecorder,
    FakeExecutionQueuePort,
    FakeExecutionReservations,
    FakeGuardrailSetRepository,
    FakePlatformReaderPort,
    FakeSpendLedger,
    FakeUnitOfWork,
)
from safent_ads.proposals.application.propose_action import ProposeAction
from safent_ads.proposals.domain.authorization import AuthorizationVerifier
from safent_ads.proposals.domain.classification import ClassificationPolicy
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

_ENTITY_REF = EntityRef(PlatformCode.META, EntityLevel.CAMPAIGN, "c-lifecycle")
_SCOPE = entity_scope(_ENTITY_REF)
_STATE_HASH = PlatformStateHash.compute({"daily_budget_minor": 10_000})
_OWNER_EMAIL = "owner@safent.example"


def _entity(
    *, status: AdEntityStatus = AdEntityStatus.ACTIVE, is_controllable: bool = True
) -> AdEntity:
    return AdEntity(
        business_id=BusinessId.new(),
        entity_ref=_ENTITY_REF,
        parent_ref=EntityRef(PlatformCode.META, EntityLevel.ACCOUNT, "act_1"),
        name="Campana panel",
        status=status,
        platform_state_hash=_STATE_HASH,
        is_controllable=is_controllable,
        budget=Budget(amount=AccountsMoney(10_000, "EUR"), kind=BudgetKind.DAILY),
    )


class _Scenario:
    def __init__(self, *, entity: AdEntity | None = None) -> None:
        self.ad_entities = InMemoryAdEntityRepository([entity or _entity()])
        self.brakes = FakeBrakeStatePort()
        self.proposals = FakeProposalRepository()
        self.authorizations = FakeAuthorizationRepository()
        self.guardrail_sets = FakeGuardrailSetRepository({_SCOPE.ref: guardrail_set()})
        self.spend_ledger = FakeSpendLedger({_SCOPE.ref: empty_ledger()})
        self.guardrail_evaluator = GuardrailEvaluator()
        self.queue = FakeExecutionQueuePort()
        self.recorder = FakeDecisionRecorder()
        self.platform_write = FakeAdsPlatformWritePort()
        self.clock = FixedClock(NOW)

        propose_action = ProposeAction(
            proposals=self.proposals,
            classification_policy=ClassificationPolicy(
                critical_impact_threshold=Money.of("100000")
            ),
            expiry_policy=ExpiryPolicy(),
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
        deps = EntityLifecycleDeps(
            ad_entities=self.ad_entities,
            brakes=self.brakes,
            proposals=self.proposals,
            authorizations=self.authorizations,
            propose_action=propose_action,
            guardrail_evaluator=self.guardrail_evaluator,
            guardrail_sets=self.guardrail_sets,
            spend_ledger=self.spend_ledger,
            execution_queue=self.queue,
            chokepoint=chokepoint,
            signer=FakeSignerPort(),
            clock=self.clock,
        )
        self.pause_entity = PauseEntity(deps)
        self.resume_entity = ResumeEntity(deps)
        self.delete_entity = DeleteEntity(deps)

    async def engage_brake(self, mode: BrakeMode = BrakeMode.ALL) -> None:
        brake = EmergencyBrake(scope=brake_scope_from(_SCOPE), mode=mode)
        brake.engage("incidente", NOW)
        await self.brakes.save(brake)

    async def engage_scoped_brake(self, scope: BrakeScope, mode: BrakeMode = BrakeMode.ALL) -> None:
        brake = EmergencyBrake(scope=scope, mode=mode)
        brake.engage("incidente", NOW)
        await self.brakes.save(brake)


class TestBrakeEngagedRefuses:
    async def test_pause_denies_when_brake_engaged(self) -> None:
        scenario = _Scenario()
        await scenario.engage_brake()

        with pytest.raises(EntityActionDeniedError) as excinfo:
            await scenario.pause_entity.execute(
                PauseEntityCommand(entity_ref=_ENTITY_REF, owner_email=_OWNER_EMAIL)
            )

        assert excinfo.value.code is EntityActionDenialCode.BRAKE_ENGAGED
        assert len(scenario.proposals.all()) == 0

    async def test_delete_denies_when_brake_engaged(self) -> None:
        scenario = _Scenario()
        await scenario.engage_brake()

        with pytest.raises(EntityActionDeniedError) as excinfo:
            await scenario.delete_entity.execute(
                DeleteEntityCommand(entity_ref=_ENTITY_REF, owner_email=_OWNER_EMAIL)
            )

        assert excinfo.value.code is EntityActionDenialCode.BRAKE_ENGAGED

    async def test_autonomous_only_brake_does_not_block_owner_action(self) -> None:
        """FR-14: `mode=AUTONOMOUS` solo bloquea `rule_authorization` -- una
        accion del propietario (`human_approval`) sigue adelante, mismo
        criterio que `SubmitApproval`/`ExecutionChokepoint`."""
        scenario = _Scenario()
        await scenario.engage_brake(mode=BrakeMode.AUTONOMOUS)

        result = await scenario.pause_entity.execute(
            PauseEntityCommand(entity_ref=_ENTITY_REF, owner_email=_OWNER_EMAIL)
        )

        assert result.outcome is ExecutionStatus.EXECUTED

    async def test_pause_denies_when_global_brake_engaged(self) -> None:
        """BUG corregido: solo se comprobaba el freno de la cuenta -- uno
        GLOBAL activo dejaba pasar la pausa del propietario igualmente."""
        scenario = _Scenario()
        await scenario.engage_scoped_brake(BrakeScope(kind=BrakeScopeKind.GLOBAL))

        with pytest.raises(EntityActionDeniedError) as excinfo:
            await scenario.pause_entity.execute(
                PauseEntityCommand(entity_ref=_ENTITY_REF, owner_email=_OWNER_EMAIL)
            )

        assert excinfo.value.code is EntityActionDenialCode.BRAKE_ENGAGED

    async def test_delete_denies_when_business_brake_engaged(self) -> None:
        scenario = _Scenario()
        scenario.brakes.link_account_to_business(_SCOPE.ref, "negocio-1")
        business_scope = BrakeScope(kind=BrakeScopeKind.BUSINESS, ref="negocio-1")
        await scenario.engage_scoped_brake(business_scope)

        with pytest.raises(EntityActionDeniedError) as excinfo:
            await scenario.delete_entity.execute(
                DeleteEntityCommand(entity_ref=_ENTITY_REF, owner_email=_OWNER_EMAIL)
            )

        assert excinfo.value.code is EntityActionDenialCode.BRAKE_ENGAGED

    async def test_another_businesss_brake_does_not_deny(self) -> None:
        scenario = _Scenario()
        scenario.brakes.link_account_to_business(_SCOPE.ref, "negocio-1")
        other_business_scope = BrakeScope(kind=BrakeScopeKind.BUSINESS, ref="otro-negocio")
        await scenario.engage_scoped_brake(other_business_scope)

        result = await scenario.pause_entity.execute(
            PauseEntityCommand(entity_ref=_ENTITY_REF, owner_email=_OWNER_EMAIL)
        )

        assert result.outcome is ExecutionStatus.EXECUTED


class TestNotControllableRefuses:
    async def test_pause_denies_when_not_controllable(self) -> None:
        scenario = _Scenario(entity=_entity(is_controllable=False))

        with pytest.raises(EntityActionDeniedError) as excinfo:
            await scenario.pause_entity.execute(
                PauseEntityCommand(entity_ref=_ENTITY_REF, owner_email=_OWNER_EMAIL)
            )

        assert excinfo.value.code is EntityActionDenialCode.NOT_CONTROLLABLE

    async def test_resume_denies_when_entity_is_active(self) -> None:
        scenario = _Scenario(entity=_entity(status=AdEntityStatus.ACTIVE))

        with pytest.raises(EntityActionDeniedError) as excinfo:
            await scenario.resume_entity.execute(
                ResumeEntityCommand(entity_ref=_ENTITY_REF, owner_email=_OWNER_EMAIL)
            )

        assert excinfo.value.code is EntityActionDenialCode.ALREADY_IN_TARGET_STATE

    async def test_resume_denies_when_not_controllable(self) -> None:
        scenario = _Scenario(
            entity=_entity(status=AdEntityStatus.PAUSED, is_controllable=False)
        )

        with pytest.raises(EntityActionDeniedError) as excinfo:
            await scenario.resume_entity.execute(
                ResumeEntityCommand(entity_ref=_ENTITY_REF, owner_email=_OWNER_EMAIL)
            )

        assert excinfo.value.code is EntityActionDenialCode.NOT_CONTROLLABLE

    async def test_pause_denies_when_already_paused(self) -> None:
        scenario = _Scenario(entity=_entity(status=AdEntityStatus.PAUSED))

        with pytest.raises(EntityActionDeniedError) as excinfo:
            await scenario.pause_entity.execute(
                PauseEntityCommand(entity_ref=_ENTITY_REF, owner_email=_OWNER_EMAIL)
            )

        assert excinfo.value.code is EntityActionDenialCode.ALREADY_IN_TARGET_STATE

    async def test_delete_denies_when_not_controllable(self) -> None:
        scenario = _Scenario(entity=_entity(is_controllable=False))

        with pytest.raises(EntityActionDeniedError) as excinfo:
            await scenario.delete_entity.execute(
                DeleteEntityCommand(entity_ref=_ENTITY_REF, owner_email=_OWNER_EMAIL)
            )

        assert excinfo.value.code is EntityActionDenialCode.NOT_CONTROLLABLE

    async def test_delete_denies_when_already_removed(self) -> None:
        scenario = _Scenario(entity=_entity(status=AdEntityStatus.REMOVED))

        with pytest.raises(EntityActionDeniedError) as excinfo:
            await scenario.delete_entity.execute(
                DeleteEntityCommand(entity_ref=_ENTITY_REF, owner_email=_OWNER_EMAIL)
            )

        assert excinfo.value.code is EntityActionDenialCode.ALREADY_IN_TARGET_STATE


class TestUnknownEntityRaises:
    async def test_pause_raises_when_entity_missing(self) -> None:
        scenario = _Scenario()
        missing_ref = EntityRef(PlatformCode.META, EntityLevel.CAMPAIGN, "does-not-exist")

        with pytest.raises(UnknownEntityError):
            await scenario.pause_entity.execute(
                PauseEntityCommand(entity_ref=missing_ref, owner_email=_OWNER_EMAIL)
            )


class TestPauseRecordsAnUndoableExecution:
    async def test_pause_transitions_status_and_leaves_an_undo_window(self) -> None:
        scenario = _Scenario()

        result = await scenario.pause_entity.execute(
            PauseEntityCommand(entity_ref=_ENTITY_REF, owner_email=_OWNER_EMAIL)
        )

        assert result.outcome is ExecutionStatus.EXECUTED
        assert result.undo_deadline is not None
        assert result.undo_deadline > NOW
        assert scenario.platform_write.call_count == 1
        command, _authorization, _key = scenario.platform_write.calls[0]
        assert command.parameter == "status"
        assert command.value == "PAUSED"

    async def test_resume_transitions_status_and_leaves_an_undo_window(self) -> None:
        scenario = _Scenario(entity=_entity(status=AdEntityStatus.PAUSED))

        result = await scenario.resume_entity.execute(
            ResumeEntityCommand(entity_ref=_ENTITY_REF, owner_email=_OWNER_EMAIL)
        )

        assert result.outcome is ExecutionStatus.EXECUTED
        assert result.undo_deadline is not None
        command, _authorization, _key = scenario.platform_write.calls[0]
        assert command.value == "ACTIVE"


class TestDeleteIsNeverUndoable:
    async def test_delete_transitions_status_without_an_undo_window(self) -> None:
        scenario = _Scenario()

        result = await scenario.delete_entity.execute(
            DeleteEntityCommand(entity_ref=_ENTITY_REF, owner_email=_OWNER_EMAIL)
        )

        assert result.outcome is ExecutionStatus.EXECUTED
        assert result.undo_deadline is None
        command, _authorization, _key = scenario.platform_write.calls[0]
        assert command.value == "DELETED"
