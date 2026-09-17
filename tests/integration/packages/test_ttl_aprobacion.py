"""T125 (AL-6): `PACKAGE_APPROVAL_TTL` = 30 minutos, reloj fijo en las
pruebas (nunca `now()` de la BD). Dos caminos:

1. `resume` despues de caducado el sobre -- ya implementado
   (`ResumePackagePublication`, `PackageApprovalExpiredError` -> 409
   `PACKAGE_APPROVAL_EXPIRED`): exige volver a aprobar, nunca re-acuña la
   firma humana en silencio.
2. Un paso de escritura que intenta EJECUTARSE (`RunPackagePublication` ->
   `ChokepointStepExecutor`) con el sobre ya caducado, SIN que la
   publicacion haya pasado nunca por `halted` -- `RunPackagePublication.
   _pre_flight_halt_reason` no comprueba `approval_expires_at` en absoluto
   (solo huella viva y freno), asi que el worker llega hasta `sign_
   authorization`, que SI rechaza firmar un paso sobre un sobre caducado
   (`PackageStepAuthorizationInvariantError`) -- pero esa excepcion escapa
   de `RunPackagePublication.execute()` sin capturar, exactamente la misma
   clase de regresion que B1 (revision de codigo, 15-sep): una publicacion
   que llega tarde a un paso detiene el CICLO ENTERO del worker en vez de
   marcarse a si misma como fallida."""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.application.ports import PlatformAssetHandle
from safent_ads.accounts.application.refresh_registered_entity_state import (
    RefreshRegisteredEntityState,
)
from safent_ads.accounts.application.register_created_entity import RegisterCreatedEntity
from safent_ads.accounts.testing.in_memory_repositories import InMemoryAdEntityRepository
from safent_ads.execution.application.chokepoint import ExecutionChokepoint
from safent_ads.execution.application.platform_state_revalidator import PlatformStateRevalidator
from safent_ads.execution.domain.guardrails import GuardrailEvaluator
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
from safent_ads.packages.application.approve_campaign_package import (
    ApproveCampaignPackage,
    ApproveCampaignPackageCommand,
)
from safent_ads.packages.application.errors import PackageApprovalExpiredError
from safent_ads.packages.application.ports import StepExecutionOutcome
from safent_ads.packages.application.resume_package_publication import (
    ResumePackagePublication,
    ResumePackagePublicationCommand,
)
from safent_ads.packages.application.run_package_publication import RunPackagePublication
from safent_ads.packages.domain.campaign_package import CampaignPackage
from safent_ads.packages.domain.identifiers import OfferingId
from safent_ads.packages.domain.step_binding import PackageStepBinding
from safent_ads.packages.infrastructure.chokepoint_step_executor import ChokepointStepExecutor
from safent_ads.packages.infrastructure.sql_package_authorization_repository import (
    SqlPackageAuthorizationRepository,
)
from safent_ads.packages.infrastructure.sql_package_repository import SqlCampaignPackageRepository
from safent_ads.packages.infrastructure.sql_package_step_repository import (
    SqlPackageStepRepository,
)
from safent_ads.packages.infrastructure.sql_publication_repository import (
    SqlPackagePublicationRepository,
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
from tests.conftest import BusinessFactory, OwnerFactory
from tests.integration.packages.conftest import SeededScope, seed_package_scope
from tests.unit.packages.application.test_approve_campaign_package import _wide_open_guardrails
from tests.unit.packages.domain.conftest import (
    NOW,
    meta_ad,
    meta_ad_set,
    propose_meta_package,
)

pytestmark = pytest.mark.integration

_CANCEL_GRACE_ELAPSED = NOW + timedelta(seconds=46)
_TTL_EXPIRED = NOW + timedelta(minutes=31)
_MEDIA = b"tiny-fake-png-bytes"
_CHECKSUM = hashlib.sha256(_MEDIA).hexdigest()


def _done(created_entity_ref: str | None = None) -> StepExecutionOutcome:
    return StepExecutionOutcome(
        state="done", created_entity_ref=created_entity_ref, outcome_code=None
    )


class _FakeStepExecutor:
    def __init__(self, outcomes: dict[int, StepExecutionOutcome]) -> None:
        self._outcomes = outcomes

    async def execute_step(
        self, *, binding: PackageStepBinding, **_kwargs: object
    ) -> StepExecutionOutcome:
        return self._outcomes[binding.step_index]


class Scenario:
    def __init__(self, session: AsyncSession, scope: SeededScope) -> None:
        self.session = session
        self.scope = scope
        self.package: CampaignPackage = propose_meta_package(
            business=scope.business_id,
            account=scope.account_ref,
            offering_id=OfferingId(scope.offering_id),
            ad_sets=(meta_ad_set(ads=(meta_ad(checksum=_CHECKSUM),)),),
        )
        self.packages = SqlCampaignPackageRepository(session)
        self.publications = SqlPackagePublicationRepository(session)
        self.steps = SqlPackageStepRepository(session)
        self.brakes = FakeBrakeStatePort()
        self.clock = FixedClock(NOW)

    async def approve(self) -> str:
        await self.packages.add(self.package)
        await self.session.flush()
        account_scope = str(self.package.account_ref)
        approve = ApproveCampaignPackage(
            packages=self.packages,
            publications=self.publications,
            authorizations=SqlPackageAuthorizationRepository(self.session),
            brakes=self.brakes,
            guardrail_evaluator=GuardrailEvaluator(),
            guardrail_sets=FakeGuardrailSetRepository(
                {account_scope: _wide_open_guardrails(account_scope)}
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
        await self.session.flush()
        return result.publication_id

    def use_case_with_fake_executor(
        self, outcomes: dict[int, StepExecutionOutcome]
    ) -> RunPackagePublication:
        entities = InMemoryAdEntityRepository()
        return RunPackagePublication(
            packages=self.packages,
            publications=self.publications,
            steps=self.steps,
            step_executor=_FakeStepExecutor(outcomes),
            brakes=self.brakes,
            entity_registration=RegisterCreatedEntity(entities),
            entity_activation_refresh=RefreshRegisteredEntityState(entities),
            clock=self.clock,
        )

    def use_case_with_real_executor(self) -> RunPackagePublication:
        """El MISMO camino de escritura real que `ads-worker` usa (T024):
        `ProposeAction` + `ExecutionChokepoint`, dobles en memoria solo en
        los puertos de I/O -- necesario para que `sign_authorization`
        (donde AL-6 se comprueba de verdad) se ejecute de verdad."""
        clock = self.clock
        proposals = FakeProposalRepository()
        authorizations = FakeAuthorizationRepository()
        execution_queue = FakeExecutionQueuePort()
        account_scope = str(self.package.account_ref)
        guardrail_sets = FakeGuardrailSetRepository(
            {account_scope: _wide_open_guardrails(account_scope)}
        )
        spend_ledger = FakeSpendLedger()
        propose_action = ProposeAction(
            proposals=proposals,
            classification_policy=ClassificationPolicy(
                critical_impact_threshold=Money.of("999999")
            ),
            expiry_policy=ExpiryPolicy(),
            clock=clock,
        )
        chokepoint = ExecutionChokepoint(
            reservations=FakeExecutionReservations(),
            queue=execution_queue,
            uow=FakeUnitOfWork(),
            brakes=self.brakes,
            proposals=proposals,
            authorizations=authorizations,
            auth_verifier=AuthorizationVerifier(FakeVerifierPort(), clock),
            guardrail_evaluator=GuardrailEvaluator(),
            guardrail_sets=guardrail_sets,
            spend_ledger=spend_ledger,
            revalidator=PlatformStateRevalidator(FakePlatformReaderPort()),
            platform_write=FakeAdsPlatformWritePort(),
            recorder=FakeDecisionRecorder(),
            clock=clock,
        )
        executor = ChokepointStepExecutor(
            proposals=proposals,
            authorizations=authorizations,
            propose_action=propose_action,
            execution_queue=execution_queue,
            chokepoint=chokepoint,
            guardrail_evaluator=GuardrailEvaluator(),
            guardrail_sets=guardrail_sets,
            spend_ledger=spend_ledger,
            platform=_FakeUploadPlatform(),
            creative_bytes=_FakeCreativeBytes(
                {str(self.package.ad_sets[0].ads[0].creative.asset_id): _MEDIA}
            ),
            signer=FakeSignerPort(),
            clock=clock,
        )
        entities = InMemoryAdEntityRepository()
        return RunPackagePublication(
            packages=self.packages,
            publications=self.publications,
            steps=self.steps,
            step_executor=executor,
            brakes=self.brakes,
            entity_registration=RegisterCreatedEntity(entities),
            entity_activation_refresh=RefreshRegisteredEntityState(entities),
            clock=clock,
        )


class _FakeUploadPlatform:
    """Doble minimo de `AdsPlatformPort`: solo `upload_asset`, con un
    `preview_url` valido (B4: HTTPS, `graph.facebook.com`)."""

    async def upload_asset(self, request: object) -> PlatformAssetHandle:
        del request
        return PlatformAssetHandle(
            platform_asset_id="meta-image-hash-abc",
            preview_url="https://graph.facebook.com/v20.0/preview.png",
        )


class _FakeCreativeBytes:
    def __init__(self, media_by_asset_id: dict[str, bytes]) -> None:
        self._media = media_by_asset_id

    async def get_bytes(self, *, business_id: object, asset_id: str) -> bytes | None:
        del business_id
        return self._media.get(asset_id)


async def test_resume_after_the_envelope_expired_is_refused(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    owner_id = await owner_factory.create()
    business_id = await business_factory.create()
    scope = await seed_package_scope(db_session, owner_id=owner_id, business_id=business_id)
    scenario = Scenario(db_session, scope)
    publication_id = await scenario.approve()
    scenario.clock.advance_to(_CANCEL_GRACE_ELAPSED)

    # Un fallo real detiene la saga -- publicacion `halted`, paquete
    # `partially_published` (la campaña SI se creo), reanudable en
    # principio.
    run = scenario.use_case_with_fake_executor(
        {
            0: _done("meta-image-hash-abc"),
            1: _done("meta:campaign:123456"),
            2: StepExecutionOutcome(
                state="failed", created_entity_ref=None, outcome_code="ad_set_creation_failed"
            ),
        }
    )
    await run.execute(publication_id)
    await run.execute(publication_id)
    result = await run.execute(publication_id)
    assert result.publication_state == "halted"

    scenario.clock.advance_to(_TTL_EXPIRED)
    resume = ResumePackagePublication(
        packages=scenario.packages, publications=scenario.publications, clock=scenario.clock
    )

    with pytest.raises(PackageApprovalExpiredError):
        await resume.execute(
            ResumePackagePublicationCommand(
                business_id=scope.business_id,
                package_id=scenario.package.package_id,
                package_hash=scenario.package.package_hash.value,
                resumed_by="owner@example.com",
            )
        )


async def test_a_step_that_tries_to_run_after_the_envelope_expired_is_refused_gracefully(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    owner_id = await owner_factory.create()
    business_id = await business_factory.create()
    scope = await seed_package_scope(db_session, owner_id=owner_id, business_id=business_id)
    scenario = Scenario(db_session, scope)
    publication_id = await scenario.approve()
    scenario.clock.advance_to(_CANCEL_GRACE_ELAPSED)

    run = scenario.use_case_with_real_executor()
    await run.execute(publication_id)  # paso 0 (UPLOAD_CREATIVE): sin autorizacion, no expira aqui

    # El sobre (30 min de TTL) caduca antes de que el worker llegue al
    # siguiente paso -- ningun halt de por medio, la publicacion sigue
    # `running`.
    scenario.clock.advance_to(_TTL_EXPIRED)

    result = await run.execute(publication_id)  # paso 1 (CREATE_CAMPAIGN)

    assert result.publication_state == "halted"
    record = await scenario.publications.get_by_id(publication_id)
    assert record is not None
    assert record.halt_reason == "package_approval_expired"
