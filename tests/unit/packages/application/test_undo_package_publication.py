"""`UndoPackagePublication` (T025): dos ventanas -- cancelar antes de
escribir (FR-08) y pausar tras la activacion (el clic ES la autorizacion
humana, mismo camino que `PauseEntity`). Nunca borra."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.execution.application.entity_lifecycle_actions import (
    EntityActionDenialCode,
    EntityActionDeniedError,
    EntityActionResult,
    PauseEntityCommand,
)
from safent_ads.execution.domain.execution_attempt import ExecutionStatus
from safent_ads.execution.domain.guardrails import GuardrailEvaluator
from safent_ads.execution.domain.identifiers import ExecutionId
from safent_ads.execution.testing.fakes import (
    FakeBrakeStatePort,
    FakeGuardrailSetRepository,
    FakeSpendLedger,
)
from safent_ads.packages.application.approve_campaign_package import (
    ApproveCampaignPackage,
    ApproveCampaignPackageCommand,
)
from safent_ads.packages.application.errors import (
    PackageChangedError,
    UndoNoConfirmedCampaignError,
    UndoPauseDeniedError,
    UndoWindowClosedError,
)
from safent_ads.packages.application.ports import PackageStepRecord
from safent_ads.packages.application.undo_package_publication import (
    UndoPackagePublication,
    UndoPackagePublicationCommand,
)
from safent_ads.packages.domain.campaign_package import ActivatedOutcome, PackageState
from safent_ads.proposals.testing.fakes import FakeSignerPort
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import EntityRef

from ..domain.conftest import propose_meta_package
from .conftest import (
    FakeCampaignPackageRepository,
    FakePackageAuthorizationRepository,
    FakePackagePublicationRepository,
    FakePackageStepRepository,
)
from .test_approve_campaign_package import _wide_open_guardrails

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
_CAMPAIGN_REF = "meta:campaign:123456"


class FakePauseEntity:
    def __init__(self, *, deny: EntityActionDeniedError | None = None) -> None:
        self.calls: list[PauseEntityCommand] = []
        self._deny = deny

    async def execute(self, command: PauseEntityCommand) -> EntityActionResult:
        self.calls.append(command)
        if self._deny is not None:
            raise self._deny
        return EntityActionResult(
            execution_id=ExecutionId.new(), outcome=ExecutionStatus.EXECUTED, undo_deadline=None
        )


class Scenario:
    def __init__(self) -> None:
        self.package = propose_meta_package(now=NOW, ttl_hours=72)
        self.packages = FakeCampaignPackageRepository()
        self.packages.by_id[str(self.package.package_id)] = self.package
        self.publications = FakePackagePublicationRepository()
        self.steps = FakePackageStepRepository()
        self.brakes = FakeBrakeStatePort()
        self.clock = FixedClock(NOW)
        self.pause_entity = FakePauseEntity()
        self.publication_id = ""

    async def approve(self) -> None:
        approve = ApproveCampaignPackage(
            packages=self.packages,
            publications=self.publications,
            authorizations=FakePackageAuthorizationRepository(),
            brakes=self.brakes,
            guardrail_evaluator=GuardrailEvaluator(),
            guardrail_sets=FakeGuardrailSetRepository(
                {
                    str(self.package.account_ref): _wide_open_guardrails(
                        str(self.package.account_ref)
                    )
                }
            ),
            spend_ledger=FakeSpendLedger(),
            signer=FakeSignerPort(),
            clock=self.clock,
        )
        result = await approve.execute(
            ApproveCampaignPackageCommand(
                business_id=self.package.business_id,
                package_id=self.package.package_id,
                package_hash=self.package.package_hash.value,
                approved_by="owner-1",
            )
        )
        self.publication_id = result.publication_id

    async def mark_published(self) -> None:
        record = await self.publications.get_by_id(self.publication_id)
        assert record is not None
        for step in record.envelope.step_plan:
            is_campaign = step.step_kind.value == "CREATE_CAMPAIGN"
            await self.steps.upsert(
                PackageStepRecord(
                    publication_id=self.publication_id,
                    step_index=step.step_index,
                    kind=step.step_kind.value.lower(),
                    local_ref=step.local_ref,
                    parent_local_ref=step.parent_local_ref,
                    state="done",
                    created_entity_ref=_CAMPAIGN_REF if is_campaign else None,
                )
            )
        self.package.begin_publishing(self.clock.now())
        self.package.record_publication_outcome(
            ActivatedOutcome(
                campaign_entity_ref=_CAMPAIGN_REF,
                activated_at=self.clock.now(),
                undo_deadline=self.clock.now() + timedelta(hours=2),
            ),
            self.clock.now(),
        )
        await self.publications.advance(
            self.publication_id,
            cursor=len(record.envelope.step_plan),
            state="completed",
            halt_reason=None,
            failed_step_index=None,
            finished_at=self.clock.now(),
        )

    def use_case(self) -> UndoPackagePublication:
        return UndoPackagePublication(
            packages=self.packages,
            publications=self.publications,
            steps=self.steps,
            pause_entity=self.pause_entity,  # type: ignore[arg-type]
            clock=self.clock,
        )

    def _command(self, **overrides: object) -> UndoPackagePublicationCommand:
        defaults: dict[str, object] = {
            "business_id": self.package.business_id,
            "package_id": self.package.package_id,
            "package_hash": self.package.package_hash.value,
            "owner_email": "owner@example.com",
            "reason": "me arrepenti",
        }
        defaults.update(overrides)
        return UndoPackagePublicationCommand(**defaults)  # type: ignore[arg-type]


class TestCancelWithinTheFortyFiveSecondWindow:
    async def test_invalidates_the_package_and_halts_the_publication(self) -> None:
        scenario = Scenario()
        await scenario.approve()

        result = await scenario.use_case().execute(scenario._command())

        assert result.undo_kind == "cancelled_publication"
        assert scenario.package.state is PackageState.INVALIDATED
        record = await scenario.publications.get_by_id(scenario.publication_id)
        assert record is not None
        assert record.state == "halted"
        assert scenario.pause_entity.calls == []


class TestPauseAfterActivation:
    async def test_pauses_the_confirmed_campaign_never_the_entity_the_client_might_claim(
        self,
    ) -> None:
        scenario = Scenario()
        await scenario.approve()
        await scenario.mark_published()

        result = await scenario.use_case().execute(scenario._command())

        assert result.undo_kind == "campaign_paused"
        assert result.campaign_entity_ref == _CAMPAIGN_REF
        assert scenario.pause_entity.calls[0].entity_ref == EntityRef.parse(_CAMPAIGN_REF)


class TestUndoDenials:
    async def test_rejects_when_the_live_hash_does_not_match(self) -> None:
        scenario = Scenario()
        await scenario.approve()

        with pytest.raises(PackageChangedError):
            await scenario.use_case().execute(scenario._command(package_hash="f" * 64))

    async def test_window_closed_when_neither_grace_nor_activation_applies(self) -> None:
        scenario = Scenario()
        await scenario.approve()
        scenario.clock.advance_to(NOW + timedelta(seconds=46))

        with pytest.raises(UndoWindowClosedError):
            await scenario.use_case().execute(scenario._command())

    async def test_undo_is_not_available_mid_saga_before_activation(self) -> None:
        """Decision de contrato (revision de codigo): `PUBLISHING`/
        `VERIFYING` no tienen ventana de deshacer -- nada gasta todavia
        (invariante 7: nace en pausa) y el freno cubre la parada de
        emergencia de este tramo (AL-1)."""
        scenario = Scenario()
        await scenario.approve()
        scenario.clock.advance_to(NOW + timedelta(seconds=46))
        scenario.package.begin_publishing(scenario.clock.now())

        with pytest.raises(UndoWindowClosedError):
            await scenario.use_case().execute(scenario._command())

    async def test_pause_denial_from_pause_entity_is_a_distinct_error_from_window_closed(
        self,
    ) -> None:
        """M5 (revision de codigo): la campana SI esta confirmada -- la
        denegacion es de `PauseEntity`, no de la ventana de deshacer."""
        scenario = Scenario()
        await scenario.approve()
        await scenario.mark_published()
        scenario.pause_entity = FakePauseEntity(
            deny=EntityActionDeniedError(EntityActionDenialCode.ALREADY_IN_TARGET_STATE, "x")
        )

        with pytest.raises(UndoPauseDeniedError):
            await scenario.use_case().execute(scenario._command())

    async def test_campaign_not_yet_confirmed_is_a_distinct_error_from_window_closed(self) -> None:
        """M5: `_confirmed_campaign_ref` exige `state == "done"` explicito
        -- un paso `running`/`unknown` con `created_entity_ref` a medias
        nunca deberia ocurrir, pero si ocurriera, no debe pausar nada."""
        scenario = Scenario()
        await scenario.approve()
        await scenario.mark_published()
        record = await scenario.publications.get_by_id(scenario.publication_id)
        assert record is not None
        campaign_step = next(
            step for step in record.envelope.step_plan if step.step_kind.value == "CREATE_CAMPAIGN"
        )
        await scenario.steps.upsert(
            PackageStepRecord(
                publication_id=scenario.publication_id,
                step_index=campaign_step.step_index,
                kind="create_campaign",
                local_ref=campaign_step.local_ref,
                parent_local_ref=campaign_step.parent_local_ref,
                state="unknown",
                created_entity_ref=_CAMPAIGN_REF,
            )
        )

        with pytest.raises(UndoNoConfirmedCampaignError):
            await scenario.use_case().execute(scenario._command())
        assert scenario.pause_entity.calls == []
