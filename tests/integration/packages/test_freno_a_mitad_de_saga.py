"""T120 (AL-1): el freno de emergencia detiene una saga de paquete ENTRE
pasos -- `RunPackagePublication` relee `EmergencyBrake.get_effective` antes
de cada paso, y `EmergencyBrake.blocks(PACKAGE_STEP)` es `True` en ambos
modos (`ALL`/`AUTONOMOUS`). Frenar a mitad de camino nunca activa la
campaña (la activacion es siempre el ultimo paso) y no toca el paso
siguiente en absoluto."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.application.refresh_registered_entity_state import (
    RefreshRegisteredEntityState,
)
from safent_ads.accounts.application.register_created_entity import RegisterCreatedEntity
from safent_ads.accounts.testing.in_memory_repositories import InMemoryAdEntityRepository
from safent_ads.execution.domain.guardrails import (
    BrakeMode,
    BrakeScope,
    BrakeScopeKind,
    EmergencyBrake,
    GuardrailEvaluator,
)
from safent_ads.execution.testing.fakes import (
    FakeBrakeStatePort,
    FakeGuardrailSetRepository,
    FakeSpendLedger,
)
from safent_ads.packages.application.approve_campaign_package import (
    ApproveCampaignPackage,
    ApproveCampaignPackageCommand,
)
from safent_ads.packages.application.ports import StepExecutionOutcome
from safent_ads.packages.application.run_package_publication import RunPackagePublication
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


def _done(created_entity_ref: str | None = None) -> StepExecutionOutcome:
    return StepExecutionOutcome(
        state="done", created_entity_ref=created_entity_ref, outcome_code=None
    )


class _FakeStepExecutor:
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
    ) -> tuple[RunPackagePublication, _FakeStepExecutor]:
        executor = _FakeStepExecutor(outcomes)
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


async def test_cero_entidades_activas_tras_frenar_en_el_paso_3(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    owner_id = await owner_factory.create()
    business_id = await business_factory.create()
    scope = await seed_package_scope(db_session, owner_id=owner_id, business_id=business_id)
    scenario = Scenario(db_session, scope)
    publication_id = await scenario.approve()
    scenario.clock.advance_to(_CANCEL_GRACE_ELAPSED)

    # Pasos 0 (UPLOAD_CREATIVE), 1 (CREATE_CAMPAIGN) y 2 (CREATE_AD_SET)
    # terminan bien -- todavia nada activo, la campaña sigue en pausa.
    run, executor = scenario.use_case(
        {
            0: _done("meta-image-hash-abc"),
            1: _done("meta:campaign:123456"),
            2: _done("meta:ad_set:as-1"),
        }
    )
    for _ in range(3):
        await run.execute(publication_id)
    for step_index in range(3):
        record = await scenario.steps.get(publication_id, step_index)
        assert record is not None
        assert record.state == "done"

    # El dueño pulsa "Parar cambios" justo antes del paso 3 (CREATE_AD).
    scenario.brakes.engage_now(
        EmergencyBrake(
            scope=BrakeScope(kind=BrakeScopeKind.GLOBAL),
            mode=BrakeMode.AUTONOMOUS,
            engaged=True,
        )
    )

    result = await run.execute(publication_id)

    assert result.publication_state == "halted"
    assert result.package_state == PackageState.PARTIALLY_PUBLISHED.value
    # El paso 3 nunca se intento: el freno se comprueba ANTES de resolver
    # el padre/ejecutar, no despues de un intento fallido.
    assert 3 not in executor.calls
    assert await scenario.steps.get(publication_id, 3) is None

    reloaded_publication = await scenario.publications.get_by_id(publication_id)
    assert reloaded_publication is not None
    assert reloaded_publication.state == "halted"

    reloaded_package = await scenario.packages.get(
        scenario.package.package_id, business_id=scope.business_id
    )
    assert reloaded_package is not None
    # Ninguna campaña activa: `PARTIALLY_PUBLISHED` (nunca `PUBLISHED`) es
    # justo la prueba -- la activacion es siempre el ultimo paso (4) y la
    # saga nunca llego siquiera al anuncio (3).
    assert reloaded_package.state is PackageState.PARTIALLY_PUBLISHED
