"""T122 (AL-3): dos garantias de concurrencia entre el ciclo generico de
`ads-worker` (`ExecutionCycle`) y `RunPackagePublication`.

1. `claim_next()` SIN `proposal_id` (el ciclo generico) nunca reclama una
   fila con `package_publication_id` relleno -- solo `RunPackagePublication`
   avanza un paso de paquete, pidiendolo por `proposal_id` explicito.
2. `RunPackagePublication` nunca decide si un paso "ya se intento" por
   estado en memoria: lee `existing_step`/`proposal_id` de
   `campaign_package_steps` en cada invocacion. Dos instancias
   INDEPENDIENTES de `RunPackagePublication` (dos procesos de `ads-worker`
   reales no comparten memoria entre si -- solo la base) que procesan la
   MISMA publicacion ven el MISMO `proposal_id` ya materializado sin que
   nadie se lo haya pasado en memoria."""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.application.refresh_registered_entity_state import (
    RefreshRegisteredEntityState,
)
from safent_ads.accounts.application.register_created_entity import RegisterCreatedEntity
from safent_ads.accounts.testing.in_memory_repositories import InMemoryAdEntityRepository
from safent_ads.execution.domain.execution_attempt import (
    ExecutionAttempt,
    build_idempotency_key,
)
from safent_ads.execution.domain.guardrails import GuardrailEvaluator
from safent_ads.execution.domain.identifiers import ExecutionId
from safent_ads.execution.infrastructure.sql_execution_queue import SqlExecutionQueue
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
from safent_ads.proposals.domain.authorization import (
    AuthorizationChannel,
    AuthorizationKind,
    sign_authorization,
)
from safent_ads.proposals.domain.cause import Cause, CauseKey
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.identifiers import AuthorizationId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Priority, Urgency
from safent_ads.proposals.domain.proposal import Proposal, ProposedDiff, new_proposal_id
from safent_ads.proposals.infrastructure.sql_authorization_repository import (
    SqlAuthorizationRepository,
)
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.proposals.testing.fakes import FakeSignerPort
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityRef
from tests.conftest import BusinessFactory, OwnerFactory
from tests.integration.packages.conftest import SeededScope, seed_package_scope
from tests.unit.packages.application.test_approve_campaign_package import _wide_open_guardrails
from tests.unit.packages.domain.conftest import NOW, propose_meta_package

pytestmark = pytest.mark.integration

_CANCEL_GRACE_ELAPSED = NOW + timedelta(seconds=46)


async def _seed_scheduled_proposal_and_authorization(
    session: AsyncSession, business_id: BusinessId, entity_ref: EntityRef, parameter: str
) -> tuple[Proposal, AuthorizationId]:
    diff = ProposedDiff.build(entity_ref, parameter, None, {"name": "x"})
    proposal = Proposal.raise_proposal(
        proposal_id=new_proposal_id(),
        business_id=business_id,
        diff=diff,
        classification=Classification.ROUTINE,
        cause=Cause(text="Paso de publicacion del paquete de campaña."),
        cause_key=CauseKey(entity_ref=entity_ref, rule_id="none", cause_type="package_step"),
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

    authorization_id = AuthorizationId.new()
    authorization = sign_authorization(
        authorization_id=authorization_id,
        proposal_id=proposal.proposal_id,
        kind=AuthorizationKind.HUMAN_APPROVAL,
        proposal_classification=proposal.classification,
        diff_hash=proposal.diff.diff_hash,
        guardrail_verdict_hash=hashlib.sha256(str(proposal.proposal_id).encode()).hexdigest(),
        issued_by="owner-1",
        channel=AuthorizationChannel.PANEL,
        decided_at=NOW,
        expires_at=NOW.replace(hour=23),
        signer=FakeSignerPort(),
    )
    await SqlAuthorizationRepository(session).save(authorization)
    await session.flush()
    return proposal, authorization_id


async def test_ciclo_generico_no_reclama_pasos_de_paquete(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    owner_id = await owner_factory.create()
    business_id = await business_factory.create()
    scope = await seed_package_scope(db_session, owner_id=owner_id, business_id=business_id)
    clock = FixedClock(NOW)
    queue = SqlExecutionQueue(db_session, clock)

    # `executions.package_publication_id` tiene FK a
    # `campaign_package_publications`: hace falta una publicacion real.
    scenario = Scenario(db_session, scope)
    publication_id = await scenario.approve()

    # Un paso de paquete: `package_publication_id` relleno.
    package_proposal, package_auth_id = await _seed_scheduled_proposal_and_authorization(
        db_session, scope.business_id, scope.account_ref, "new_campaign:pkg"
    )
    package_attempt = ExecutionAttempt.claim_for_package_step(
        business_id=scope.business_id,
        proposal_id=package_proposal.proposal_id,
        authorization_id=package_auth_id,
        publication_id=publication_id,
        step_index=0,
    )
    await queue.save(package_attempt)

    # Una escritura suelta, generica -- sin `package_publication_id`.
    generic_proposal, generic_auth_id = await _seed_scheduled_proposal_and_authorization(
        db_session, scope.business_id, scope.account_ref, "new_campaign:generic"
    )
    generic_attempt = ExecutionAttempt(
        execution_id=ExecutionId.new(),
        business_id=scope.business_id,
        proposal_id=generic_proposal.proposal_id,
        authorization_id=generic_auth_id,
        idempotency_key=build_idempotency_key(
            generic_proposal.proposal_id, generic_proposal.diff.diff_hash
        ),
    )
    await queue.save(generic_attempt)
    await db_session.flush()

    claimed = await queue.claim_next()

    assert claimed is not None
    assert claimed.proposal_id == generic_proposal.proposal_id
    assert claimed.package_publication_id is None

    second_claim = await queue.claim_next()
    # La fila de paquete sigue ahi, pero el ciclo generico jamas la ve --
    # ni siquiera cuando ya no queda nada mas que reclamar.
    assert second_claim is None


def _done(created_entity_ref: str | None = None) -> StepExecutionOutcome:
    return StepExecutionOutcome(
        state="done", created_entity_ref=created_entity_ref, outcome_code=None
    )


class _RecordingStepExecutor:
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

    def worker(self, outcomes: dict[int, StepExecutionOutcome]) -> _RecordingStepExecutor:
        """Una instancia NUEVA de `RunPackagePublication` con su propio
        ejecutor -- ningun estado en memoria compartido con otro `worker()`:
        exactamente lo que dos procesos distintos de `ads-worker` serian."""
        executor = _RecordingStepExecutor(outcomes)
        entities = InMemoryAdEntityRepository()
        self._last_run = RunPackagePublication(
            packages=self.packages,
            publications=self.publications,
            steps=self.steps,
            step_executor=executor,
            brakes=self.brakes,
            entity_registration=RegisterCreatedEntity(entities),
            entity_activation_refresh=RefreshRegisteredEntityState(entities),
            clock=self.clock,
        )
        return executor

    async def run(self, publication_id: str) -> object:
        return await self._last_run.execute(publication_id)


async def test_run_package_publication_lee_el_desenlace_de_la_bd(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    owner_id = await owner_factory.create()
    business_id = await business_factory.create()
    scope = await seed_package_scope(db_session, owner_id=owner_id, business_id=business_id)
    scenario = Scenario(db_session, scope)
    publication_id = await scenario.approve()
    scenario.clock.advance_to(_CANCEL_GRACE_ELAPSED)

    proposal, _auth_id = await _seed_scheduled_proposal_and_authorization(
        db_session, scope.business_id, scope.account_ref, "new_campaign:worker-a"
    )

    # "Worker A": procesa el paso 0 (UPLOAD_CREATIVE, sin Proposal) y luego
    # el paso 1 (CREATE_CAMPAIGN) -- la escritura se aplico pero la
    # respuesta se perdio (`unknown`), con el `proposal_id` que el bróker
    # ya reservo. Este objeto se descarta justo despues: nada de el
    # sobrevive salvo lo que quedo persistido.
    worker_a = scenario.worker({0: _done("meta-image-hash-abc")})
    await scenario.run(publication_id)
    worker_a.outcomes[1] = StepExecutionOutcome(
        state="unknown", created_entity_ref=None, outcome_code=None, proposal_id=str(
            proposal.proposal_id
        ),
    )
    first = await scenario.run(publication_id)
    assert first.publication_state == "running"
    assert first.package_state == PackageState.VERIFYING.value
    assert worker_a.calls[-1] == (1, None)  # primer intento: nada que reconciliar aun

    # "Worker B": una instancia COMPLETAMENTE NUEVA (nunca vio a `worker_a`
    # ni a su `proposal_id` en memoria) procesa la MISMA publicacion en el
    # siguiente tick. Si `RunPackagePublication` leyera de algun estado en
    # proceso en vez de `campaign_package_steps`, `worker_b` no tendria
    # forma de saber que el paso 1 ya tiene un `proposal_id` reservado.
    worker_b = scenario.worker({1: _done("meta:campaign:live-1")})
    second = await scenario.run(publication_id)

    assert second.publication_state == "running"
    # La prueba central: worker_b recibio el `existing_proposal_id` exacto
    # que worker_a genero -- leido de la fila persistida, nunca compartido
    # en memoria entre los dos objetos.
    assert worker_b.calls == [(1, str(proposal.proposal_id))]

    step_record = await scenario.steps.get(publication_id, 1)
    assert step_record is not None
    assert step_record.state == "done"
    assert step_record.proposal_id == str(proposal.proposal_id)
