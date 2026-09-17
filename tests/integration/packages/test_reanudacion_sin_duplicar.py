"""T109 (BL-4/AL-3): una caida entre la escritura real y su recibo confirmado
(`unknown`) nunca crea una segunda campaña al reconciliar -- la clave de
idempotencia del paso (`pkg-<publication_id>-<step_index>`) es estable
entre el intento que se quedo en el aire y el siguiente tick del worker que
lo reconcilia por lectura, y `campaign_package_steps` conserva UNA sola fila
para ese `step_index`."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.application.refresh_registered_entity_state import (
    RefreshRegisteredEntityState,
)
from safent_ads.accounts.application.register_created_entity import RegisterCreatedEntity
from safent_ads.accounts.testing.in_memory_repositories import InMemoryAdEntityRepository
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
from safent_ads.proposals.domain.cause import Cause, CauseKey
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Priority, Urgency
from safent_ads.proposals.domain.proposal import Proposal, ProposedDiff, new_proposal_id
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.proposals.testing.fakes import FakeSignerPort
from safent_ads.shared.clock import FixedClock
from tests.conftest import BusinessFactory, OwnerFactory
from tests.integration.packages.conftest import SeededScope, seed_package_scope
from tests.unit.packages.application.test_approve_campaign_package import _wide_open_guardrails
from tests.unit.packages.domain.conftest import NOW, propose_meta_package

pytestmark = pytest.mark.integration

_CANCEL_GRACE_ELAPSED = NOW + timedelta(seconds=46)


async def _seed_scheduled_proposal(session: AsyncSession, package: CampaignPackage) -> str:
    """`campaign_package_steps.proposal_id` tiene FK a `proposals` (0042):
    un `StepExecutionOutcome.proposal_id` de mentira, sin fila real detras,
    rompe esa FK. Levanta el MISMO camino que `ChokepointStepExecutor.
    _execute_write_step` ya recorre (PENDING -> APPROVED -> SCHEDULED, un
    salto por escritura, `proposals_guard_diff_hash`) para el paso
    `CREATE_CAMPAIGN` bajo prueba -- contra `package.account_ref`, el mismo
    `entity_ref` que `_to_write_step` usa de verdad para ese paso (la
    campaña todavia no existe; `proposals_entity_scope_mismatch` exige que
    el `entity_ref` ya viva en `ad_entities` o `platform_accounts`)."""
    diff = ProposedDiff.build(package.account_ref, "new_campaign:abc", None, {"name": "x"})
    proposal = Proposal.raise_proposal(
        proposal_id=new_proposal_id(),
        business_id=package.business_id,
        diff=diff,
        classification=Classification.ROUTINE,
        cause=Cause(text="Paso de publicacion del paquete de campaña."),
        cause_key=CauseKey(
            entity_ref=package.account_ref, rule_id="none", cause_type="package_step"
        ),
        evidence=(),
        estimated_impact=Money.zero(),
        priority=Priority(urgency=Urgency.RECOMMENDED),
        now=NOW,
        expires_at=NOW.replace(hour=23),
    )
    proposals = SqlProposalRepository(session)
    await proposals.save(proposal)
    proposal.approve(proposal.diff.diff_hash, NOW)
    await proposals.save(proposal)
    proposal.schedule_execution(0, NOW)
    await proposals.save(proposal)
    await session.flush()
    return str(proposal.proposal_id)


def _done(created_entity_ref: str | None = None) -> StepExecutionOutcome:
    return StepExecutionOutcome(
        state="done", created_entity_ref=created_entity_ref, outcome_code=None
    )


class _RecordingStepExecutor:
    """A diferencia del doble fijo de otros ficheros, expone un desenlace
    MUTABLE por `step_index` (se reasigna entre invocaciones, como
    `TestHaltAfterVerifyingNeverRaises` en `test_run_package_publication.py`)
    y registra el `existing_proposal_id` recibido en cada llamada -- lo que
    hace falta para probar que la SEGUNDA invocacion del mismo paso nunca
    vuelve a proponer."""

    def __init__(self, outcomes: dict[int, StepExecutionOutcome]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[int, str | None]] = []

    async def execute_step(
        self,
        *,
        binding: PackageStepBinding,
        existing_proposal_id: str | None = None,
        **_kwargs: object,
    ) -> StepExecutionOutcome:
        self.calls.append((binding.step_index, existing_proposal_id))
        return self.outcomes[binding.step_index]


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
    ) -> tuple[RunPackagePublication, _RecordingStepExecutor]:
        executor = _RecordingStepExecutor(outcomes)
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


async def test_caida_entre_escritura_y_recibo_no_crea_dos_campanas(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    owner_id = await owner_factory.create()
    business_id = await business_factory.create()
    scope = await seed_package_scope(db_session, owner_id=owner_id, business_id=business_id)
    scenario = Scenario(db_session, scope)
    publication_id = await scenario.approve()
    scenario.clock.advance_to(_CANCEL_GRACE_ELAPSED)
    real_proposal_id = await _seed_scheduled_proposal(db_session, scenario.package)
    unknown_with_proposal = StepExecutionOutcome(
        state="unknown", created_entity_ref=None, outcome_code=None, proposal_id=real_proposal_id
    )

    # Paso 0 (UPLOAD_CREATIVE) termina limpio -- no es el paso bajo prueba.
    run, executor = scenario.use_case({0: _done("meta-image-hash-abc")})
    await run.execute(publication_id)

    # Paso 1 (CREATE_CAMPAIGN): la escritura se aplico de verdad en Meta
    # pero la respuesta se perdio -- `unknown`, con el `proposal_id` que el
    # bróker ya reservo. La publicacion sigue `running` (verificando), nada
    # se detiene ni se marca como fallido.
    executor.outcomes[1] = unknown_with_proposal
    first = await run.execute(publication_id)
    assert first.publication_state == "running"
    assert first.package_state == PackageState.VERIFYING.value

    step_before_reconcile = await scenario.steps.get(publication_id, 1)
    assert step_before_reconcile is not None
    assert step_before_reconcile.state == "unknown"
    assert step_before_reconcile.proposal_id == real_proposal_id

    # El siguiente tick del worker (5 s despues, mismo publication_id, mismo
    # step_index=1): la reconciliacion por lectura ahora encuentra el
    # recibo confirmado -- `done` -- sin volver a proponer una CAMPAÑA nueva.
    executor.outcomes[1] = _done("meta:campaign:live-1")
    second = await run.execute(publication_id)

    assert second.publication_state == "running"
    step_after_reconcile = await scenario.steps.get(publication_id, 1)
    assert step_after_reconcile is not None
    assert step_after_reconcile.state == "done"
    assert step_after_reconcile.created_entity_ref == "meta:campaign:live-1"
    # El proposal_id de la campaña NUNCA cambia entre el intento perdido y
    # su reconciliacion: una segunda campaña habria significado una segunda
    # Proposal/proposal_id distinto.
    assert step_after_reconcile.proposal_id == real_proposal_id

    # La reconciliacion se pidio para el MISMO step_index (1) las dos veces,
    # y la segunda llamada llevaba el existing_proposal_id de la primera --
    # RunPackagePublication nunca trato el reintento como un paso nuevo.
    campaign_calls = [call for call in executor.calls if call[0] == 1]
    assert campaign_calls == [(1, None), (1, real_proposal_id)]
    assert await scenario.steps.get(publication_id, 2) is None  # aun no se avanzo
