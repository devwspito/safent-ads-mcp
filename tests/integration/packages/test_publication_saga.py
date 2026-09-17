"""`RunPackagePublication`/`ApproveCampaignPackage` contra Postgres real
(migracion 0042/0044/0045/0046): el mismo camino que `ChokepointStepExecutor`
recorrería, pero con un `PackageStepExecutorPort` doble ("dobles de
plataforma" del quickstart) para no necesitar un bróker/plataforma real --
lo que se prueba aquí es que `campaign_packages`/`campaign_package_
publications`/`campaign_package_steps` sobreviven un ciclo completo de
la saga: aprobar, avanzar paso a paso, un fallo a mitad sin duplicar nada
al reanudar, y las dos ventanas de deshacer."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.application.refresh_registered_entity_state import (
    RefreshRegisteredEntityState,
)
from safent_ads.accounts.application.register_created_entity import RegisterCreatedEntity
from safent_ads.accounts.testing.in_memory_repositories import InMemoryAdEntityRepository
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
from safent_ads.packages.application.errors import PackageNotResumableError
from safent_ads.packages.application.ports import StepExecutionOutcome
from safent_ads.packages.application.resume_package_publication import (
    ResumePackagePublication,
    ResumePackagePublicationCommand,
)
from safent_ads.packages.application.run_package_publication import RunPackagePublication
from safent_ads.packages.application.undo_package_publication import (
    UndoPackagePublication,
    UndoPackagePublicationCommand,
)
from safent_ads.packages.domain.campaign_package import CampaignPackage, PackageState
from safent_ads.packages.domain.identifiers import OfferingId
from safent_ads.packages.domain.step_binding import PackageStepBinding
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
from safent_ads.proposals.testing.fakes import FakeSignerPort
from safent_ads.shared.clock import FixedClock
from tests.conftest import BusinessFactory, OwnerFactory
from tests.integration.packages.conftest import SeededScope, seed_package_scope
from tests.unit.packages.application.test_approve_campaign_package import _wide_open_guardrails
from tests.unit.packages.domain.conftest import NOW, propose_meta_package

pytestmark = pytest.mark.integration

_CANCEL_GRACE_ELAPSED = NOW + timedelta(seconds=46)


class _PausedResult:
    execution_id = ExecutionId.new()
    outcome = ExecutionStatus.EXECUTED
    undo_deadline = None


class FakePauseEntityForSaga:
    """Doble minimo: `UndoPackagePublication` solo necesita `.execute`
    devolviendo algo con `.execution_id`."""

    def __init__(self) -> None:
        self.calls: list[object] = []

    async def execute(self, command: object) -> _PausedResult:
        self.calls.append(command)
        return _PausedResult()


async def _seed(
    session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> SeededScope:
    owner_id = await owner_factory.create()
    business_id = await business_factory.create()
    return await seed_package_scope(session, owner_id=owner_id, business_id=business_id)


def _done(created_entity_ref: str | None = None) -> StepExecutionOutcome:
    return StepExecutionOutcome(
        state="done", created_entity_ref=created_entity_ref, outcome_code=None
    )


class FakeStepExecutor:
    """La "plataforma falsa" del quickstart: cada paso se resuelve segun
    un guion fijado por el test, sin hablar con ningun bróker real."""

    def __init__(self, outcomes: dict[int, StepExecutionOutcome]) -> None:
        self._outcomes = outcomes
        self.calls: list[int] = []

    async def execute_step(
        self, *, binding: PackageStepBinding, **_kwargs: object
    ) -> StepExecutionOutcome:
        self.calls.append(binding.step_index)
        return self._outcomes[binding.step_index]


class Scenario:
    def __init__(self, session: AsyncSession, scope: SeededScope) -> None:
        self.session = session
        self.scope = scope
        self.package: CampaignPackage = propose_meta_package(
            business=scope.business_id,
            account=scope.account_ref,
            offering_id=OfferingId(scope.offering_id),
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

    def use_case(
        self, outcomes: dict[int, StepExecutionOutcome]
    ) -> tuple[RunPackagePublication, FakeStepExecutor]:
        executor = FakeStepExecutor(outcomes)
        entities = InMemoryAdEntityRepository()
        run_publication = RunPackagePublication(
            packages=self.packages,
            publications=self.publications,
            steps=self.steps,
            step_executor=executor,
            brakes=self.brakes,
            entity_registration=RegisterCreatedEntity(entities),
            entity_activation_refresh=RefreshRegisteredEntityState(entities),
            clock=self.clock,
        )
        return run_publication, executor


async def test_happy_path_reaches_published_with_five_real_step_rows(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    scope = await _seed(db_session, owner_factory, business_factory)
    scenario = Scenario(db_session, scope)
    publication_id = await scenario.approve()
    scenario.clock.advance_to(_CANCEL_GRACE_ELAPSED)  # pasada la gracia previa de 45 s (FR-08)
    use_case, _executor = scenario.use_case(
        {
            0: _done("meta-image-hash-1"),
            1: _done("meta:campaign:live-1"),
            2: _done("meta:ad_set:live-1"),
            3: _done("meta:ad:live-1"),
            4: _done(None),
        }
    )

    result = None
    for _ in range(5):
        result = await use_case.execute(publication_id)
        await db_session.flush()

    assert result is not None
    assert result.publication_state == "completed"
    assert result.package_state == PackageState.PUBLISHED.value
    reloaded = await scenario.packages.get(
        scenario.package.package_id, business_id=scope.business_id
    )
    assert reloaded is not None
    assert reloaded.state is PackageState.PUBLISHED
    for step_index in range(5):
        record = await scenario.steps.get(publication_id, step_index)
        assert record is not None
        assert record.state == "done"


async def test_a_failed_step_leaves_the_package_partially_published_and_resume_does_not_duplicate(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    scope = await _seed(db_session, owner_factory, business_factory)
    scenario = Scenario(db_session, scope)
    publication_id = await scenario.approve()
    scenario.clock.advance_to(_CANCEL_GRACE_ELAPSED)
    use_case, _executor = scenario.use_case(
        {
            0: _done("meta-image-hash-1"),
            1: _done("meta:campaign:live-1"),
            2: StepExecutionOutcome(
                state="failed", created_entity_ref=None, outcome_code="ad_set_creation_failed"
            ),
        }
    )

    result = None
    for _ in range(3):
        result = await use_case.execute(publication_id)
        await db_session.flush()
    assert result is not None
    assert result.publication_state == "halted"
    assert result.package_state == PackageState.PARTIALLY_PUBLISHED.value

    # Reanudar: el sobre sigue vivo (30 min de TTL, el reloj solo avanzo 46 s).
    resume = ResumePackagePublication(
        packages=scenario.packages, publications=scenario.publications, clock=scenario.clock
    )
    resumed = await resume.execute(
        ResumePackagePublicationCommand(
            business_id=scope.business_id,
            package_id=scenario.package.package_id,
            package_hash=scenario.package.package_hash.value,
            resumed_by="owner-1",
        )
    )
    await db_session.flush()
    assert resumed.publication_id == publication_id

    # El siguiente paso (CREATE_AD_SET, indice 2) ahora sí termina bien --
    # los pasos 0 y 1, ya `done`, no se vuelven a ejecutar (misma clave de
    # idempotencia, `RunPackagePublication` nunca los retoca).
    use_case_after_resume, executor_after_resume = scenario.use_case(
        {2: _done("meta:ad_set:live-1"), 3: _done("meta:ad:live-1"), 4: _done(None)}
    )
    for _ in range(3):
        result = await use_case_after_resume.execute(publication_id)
        await db_session.flush()

    assert result.publication_state == "completed"
    assert result.package_state == PackageState.PUBLISHED.value
    # Los pasos 0/1 nunca se volvieron a llamar tras la reanudacion.
    assert executor_after_resume.calls == [2, 3, 4]
    campaign_step = await scenario.steps.get(publication_id, 1)
    assert campaign_step is not None
    assert campaign_step.created_entity_ref == "meta:campaign:live-1"


async def test_undo_within_the_grace_window_cancels_without_writing_anything(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    scope = await _seed(db_session, owner_factory, business_factory)
    scenario = Scenario(db_session, scope)
    publication_id = await scenario.approve()

    undo = UndoPackagePublication(
        packages=scenario.packages,
        publications=scenario.publications,
        steps=scenario.steps,
        pause_entity=FakePauseEntityForSaga(),  # type: ignore[arg-type]
        clock=scenario.clock,
    )
    result = await undo.execute(
        UndoPackagePublicationCommand(
            business_id=scope.business_id,
            package_id=scenario.package.package_id,
            package_hash=scenario.package.package_hash.value,
            owner_email="owner@example.com",
            reason="me arrepenti",
        )
    )
    await db_session.flush()

    assert result.undo_kind == "cancelled_publication"
    reloaded = await scenario.packages.get(
        scenario.package.package_id, business_id=scope.business_id
    )
    assert reloaded is not None
    assert reloaded.state is PackageState.INVALIDATED
    record = await scenario.publications.get_by_id(publication_id)
    assert record is not None
    assert record.state == "halted"

    # Una publicacion cancelada nunca es reanudable.
    resume = ResumePackagePublication(
        packages=scenario.packages, publications=scenario.publications, clock=scenario.clock
    )
    with pytest.raises(PackageNotResumableError):
        await resume.execute(
            ResumePackagePublicationCommand(
                business_id=scope.business_id,
                package_id=scenario.package.package_id,
                package_hash=scenario.package.package_hash.value,
                resumed_by="owner-1",
            )
        )
