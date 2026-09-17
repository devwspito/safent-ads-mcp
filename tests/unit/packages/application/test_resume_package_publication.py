"""`ResumePackagePublication` (T025): reanudar es decidir otra vez (R2.10)
-- exige la huella viva y deniega si el sobre humano ya caduco, en vez de
re-acuñar la firma en silencio (AL-2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.execution.domain.guardrails import GuardrailEvaluator
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
    PackageApprovalExpiredError,
    PackageChangedError,
    PackageNotResumableError,
)
from safent_ads.packages.application.resume_package_publication import (
    ResumePackagePublication,
    ResumePackagePublicationCommand,
)
from safent_ads.packages.domain.campaign_package import (
    PackageState,
    PartialOutcome,
    UncertainOutcome,
)
from safent_ads.proposals.testing.fakes import FakeSignerPort
from safent_ads.shared.clock import FixedClock

from ..domain.conftest import propose_meta_package
from .conftest import (
    FakeCampaignPackageRepository,
    FakePackageAuthorizationRepository,
    FakePackagePublicationRepository,
)
from .test_approve_campaign_package import _wide_open_guardrails

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)


class Scenario:
    def __init__(self) -> None:
        self.package = propose_meta_package(now=NOW, ttl_hours=72)
        self.packages = FakeCampaignPackageRepository()
        self.packages.by_id[str(self.package.package_id)] = self.package
        self.publications = FakePackagePublicationRepository()
        self.brakes = FakeBrakeStatePort()
        self.clock = FixedClock(NOW)

    async def approve_and_halt(self) -> str:
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
        # Simula una saga detenida a mitad (partially_published/halted).
        self.package.begin_publishing(self.clock.now())
        self.package.record_publication_outcome(
            PartialOutcome(created_count=1, failed_step_index=2, next_step_hint="continua"),
            self.clock.now(),
        )
        await self.publications.advance(
            result.publication_id,
            cursor=2,
            state="halted",
            halt_reason="step_failed",
            failed_step_index=2,
            finished_at=self.clock.now(),
        )
        return result.publication_id

    def use_case(self) -> ResumePackagePublication:
        return ResumePackagePublication(
            packages=self.packages, publications=self.publications, clock=self.clock
        )


class TestResumeWithALiveEnvelope:
    async def test_transitions_the_publication_back_to_running(self) -> None:
        scenario = Scenario()
        publication_id = await scenario.approve_and_halt()

        result = await scenario.use_case().execute(
            ResumePackagePublicationCommand(
                business_id=scenario.package.business_id,
                package_id=scenario.package.package_id,
                package_hash=scenario.package.package_hash.value,
                resumed_by="owner-1",
            )
        )

        assert result.publication_id == publication_id
        record = await scenario.publications.get_by_id(publication_id)
        assert record is not None
        assert record.state == "running"
        assert record.cursor == 2
        # Regresion: sin `resume_publishing`, el paquete se quedaba en
        # `PARTIALLY_PUBLISHED` -- `RunPackagePublication` no podia volver a
        # llamar `record_publication_outcome` (esa transicion no existia).
        assert scenario.package.state is PackageState.PUBLISHING

    async def test_a_verifying_package_is_resumable_even_though_the_publication_never_halted(
        self,
    ) -> None:
        """M2 (revision de codigo): un paso `unknown` deja la publicacion
        en `running` (`RunPackagePublication._stay_running`, BL-4/INV-4) a
        la vez que el paquete pasa a `verifying`. contracts/api.md §4
        permite reanudar desde `verifying`; exigir `record.state ==
        'halted'` a secas lo hacia inalcanzable para siempre."""
        scenario = Scenario()
        publication_id = await scenario.approve_and_halt()
        scenario.package.resume_publishing(scenario.clock.now())
        scenario.package.record_publication_outcome(UncertainOutcome(), scenario.clock.now())
        await scenario.publications.advance(
            publication_id,
            cursor=2,
            state="running",
            halt_reason=None,
            failed_step_index=None,
            finished_at=None,
        )

        result = await scenario.use_case().execute(
            ResumePackagePublicationCommand(
                business_id=scenario.package.business_id,
                package_id=scenario.package.package_id,
                package_hash=scenario.package.package_hash.value,
                resumed_by="owner-1",
            )
        )

        assert result.publication_id == publication_id
        record = await scenario.publications.get_by_id(publication_id)
        assert record is not None
        assert record.state == "running"


class TestResumeDenials:
    async def test_rejects_when_the_live_hash_does_not_match(self) -> None:
        scenario = Scenario()
        await scenario.approve_and_halt()

        with pytest.raises(PackageChangedError):
            await scenario.use_case().execute(
                ResumePackagePublicationCommand(
                    business_id=scenario.package.business_id,
                    package_id=scenario.package.package_id,
                    package_hash="f" * 64,
                    resumed_by="owner-1",
                )
            )

    async def test_rejects_when_the_package_is_not_in_a_resumable_state(self) -> None:
        scenario = Scenario()

        with pytest.raises(PackageNotResumableError):
            await scenario.use_case().execute(
                ResumePackagePublicationCommand(
                    business_id=scenario.package.business_id,
                    package_id=scenario.package.package_id,
                    package_hash=scenario.package.package_hash.value,
                    resumed_by="owner-1",
                )
            )

    async def test_rejects_when_the_publication_is_not_halted(self) -> None:
        scenario = Scenario()
        publication_id = await scenario.approve_and_halt()
        await scenario.publications.advance(
            publication_id,
            cursor=2,
            state="running",
            halt_reason=None,
            failed_step_index=None,
            finished_at=None,
        )

        with pytest.raises(PackageNotResumableError):
            await scenario.use_case().execute(
                ResumePackagePublicationCommand(
                    business_id=scenario.package.business_id,
                    package_id=scenario.package.package_id,
                    package_hash=scenario.package.package_hash.value,
                    resumed_by="owner-1",
                )
            )

    async def test_rejects_when_the_human_envelope_has_expired(self) -> None:
        scenario = Scenario()
        await scenario.approve_and_halt()
        scenario.clock.advance_to(NOW + timedelta(minutes=31))

        with pytest.raises(PackageApprovalExpiredError):
            await scenario.use_case().execute(
                ResumePackagePublicationCommand(
                    business_id=scenario.package.business_id,
                    package_id=scenario.package.package_id,
                    package_hash=scenario.package.package_hash.value,
                    resumed_by="owner-1",
                )
            )
