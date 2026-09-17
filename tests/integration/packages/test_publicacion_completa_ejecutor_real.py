"""T123/AL-4 + 003-entidades-creadas: publicacion REAL de extremo a extremo
-- `RunPackagePublication` + `ChokepointStepExecutor` de verdad (sin
`PackageStepExecutorPort` de mentira) contra Postgres real, para un paquete
Meta con UN conjunto y UN anuncio. Sube la creatividad, crea la campaña, el
conjunto, el anuncio y activa -- las cinco filas de `campaign_package_steps`
llegan a `done`, cada paso con padre (`CREATE_AD_SET`/`CREATE_AD`/
`ACTIVATE_CAMPAIGN`) hereda su `expected_state_hash` del `confirmed_state_
hash` del recibo del padre (data-model.md R2.8 generalizado; el gap AL-4/
T123 que bloqueaba todo paquete con un conjunto/anuncio real --
`AdChildCreationError` en el primer `CREATE_AD_SET` -- queda cerrado).

Gap escalado por esta misma prueba (AL-4/T123) y cerrado por
003-entidades-creadas: `proposals_entity_exists()` (0027) exige que
`entity_ref` viva ya en `ad_entities`/`platform_accounts` antes de aceptar
CUALQUIER `Proposal` -- `CREATE_CAMPAIGN` lo cumple porque su `entity_ref`
es la cuenta (ya en `platform_accounts`), pero `CREATE_AD_SET`/`CREATE_AD`/
`ACTIVATE_CAMPAIGN` apuntan a un padre que la MISMA saga acaba de crear.
`RunPackagePublication._register_created_entity` (via `RegisterCreatedEntity`,
`accounts.application`) registra ese padre en `ad_entities` en cuanto su
recibo confirma -- ANTES de que el paso siguiente pueda proponerse -- asi
que esta prueba ya NO necesita simular que el sync periodico de cuenta se
adelanto: publica de punta a punta contra el invariante real, sin doble."""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.application.detect_platform_drift import DetectPlatformDrift
from safent_ads.accounts.application.ports import EntityStateSnapshot, PlatformAssetHandle
from safent_ads.accounts.application.refresh_registered_entity_state import (
    RefreshRegisteredEntityState,
)
from safent_ads.accounts.application.register_created_entity import RegisterCreatedEntity
from safent_ads.accounts.domain.ad_entity import AdEntityStatus
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.accounts.infrastructure.sql_repositories import SqlAdEntityRepository
from safent_ads.execution.application.chokepoint import ExecutionChokepoint
from safent_ads.execution.application.platform_state_revalidator import PlatformStateRevalidator
from safent_ads.execution.domain.guardrails import (
    BrakeMode,
    BrakeScope,
    BrakeScopeKind,
    EmergencyBrake,
    GuardrailEvaluator,
)
from safent_ads.execution.testing.fakes import (
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
from safent_ads.packages.application.resume_package_publication import (
    ResumePackagePublication,
    ResumePackagePublicationCommand,
)
from safent_ads.packages.application.run_package_publication import RunPackagePublication
from safent_ads.packages.domain.campaign_package import CampaignPackage, PackageState
from safent_ads.packages.domain.identifiers import OfferingId
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
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.proposals.testing.fakes import (
    FakeAuthorizationRepository,
    FakeSignerPort,
    FakeVerifierPort,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import EntityLevel, EntityRef
from tests.conftest import BusinessFactory, OwnerFactory
from tests.integration.packages.conftest import SeededScope, seed_package_scope
from tests.unit.accounts.application.conftest import FakeAdsPlatformPort, FakeEventBus
from tests.unit.packages.application.test_approve_campaign_package import _wide_open_guardrails
from tests.unit.packages.domain.conftest import NOW, meta_ad, meta_ad_set, propose_meta_package
from tests.unit.packages.infrastructure.test_chokepoint_step_executor import (
    _ad_set_ref,
    _campaign_ref,
    _FakeCreativeBytes,
    _FakeUploadPlatform,
    _ScriptedPlatformWrite,
)

pytestmark = pytest.mark.integration

_CANCEL_GRACE_ELAPSED = NOW + timedelta(seconds=46)
_MEDIA = b"tiny-fake-png-bytes"
_CHECKSUM = hashlib.sha256(_MEDIA).hexdigest()
_UPLOAD_STEP, _CAMPAIGN_STEP, _AD_SET_STEP, _AD_STEP, _ACTIVATE_STEP = range(5)
_EXTERNAL_ID = "123456"
# 003-entidades-creadas: `RegisterCreatedEntity` exige un sha256 hexadecimal
# real (`ad_entities.platform_state_hash` lo valida por CHECK, 0003) --
# nunca el opaco `"state-after"` de otros tests que no registran nada.
_CONFIRMED_STATE_HASH = hashlib.sha256(b"package-journey-confirmed-state").hexdigest()
_IMAGE_HASH = "b" * 32
# Item 3 (repaso 0.2.23): a diferencia de `_CONFIRMED_STATE_HASH` (un digest
# opaco), este es el hash de un `canonical_state` real y conocido -- lo que
# `DetectPlatformDrift` necesita releer y comparar contra `ad_entities`
# despues de una activacion, sin fabricar una preimagen.
_ACTIVATED_CANONICAL_STATE = {"status": "ACTIVE"}
_ACTIVATED_STATE_HASH = PlatformStateHash.compute(_ACTIVATED_CANONICAL_STATE).value


class Scenario:
    def __init__(self, session: AsyncSession, scope: SeededScope) -> None:
        self.session = session
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

    def use_case(
        self, *, confirmed_state_hash: str = _CONFIRMED_STATE_HASH
    ) -> RunPackagePublication:
        """El MISMO camino de escritura que `ads-worker` usaria de verdad
        (T024): `ProposeAction` + `ExecutionChokepoint`, con `proposals`
        Postgres real (la FK real de `campaign_package_steps.proposal_id`,
        0042, lo exige) -- dobles en memoria solo en `authorizations`
        (encadenada a la humana via `PackageApprovalProof`, no una FK) y en
        los puertos de I/O de plataforma. `confirmed_state_hash` es
        configurable (item 3, repaso 0.2.23): la prueba de deriva post-
        activacion necesita que el hash confirmado por la plataforma sea
        el hash de un `canonical_state` real, calculable de antemano --
        nunca el opaco `_CONFIRMED_STATE_HASH` por defecto."""
        clock = self.clock
        campaign_ref = _campaign_ref(self.package, _EXTERNAL_ID)
        ad_set_ref = _ad_set_ref(self.package, _EXTERNAL_ID)
        account_scope = str(self.package.account_ref)
        proposals = SqlProposalRepository(self.session)
        authorizations = FakeAuthorizationRepository()
        execution_queue = FakeExecutionQueuePort()
        guardrail_sets = FakeGuardrailSetRepository(
            {
                account_scope: _wide_open_guardrails(account_scope),
                str(campaign_ref): _wide_open_guardrails(str(campaign_ref)),
                str(ad_set_ref): _wide_open_guardrails(str(ad_set_ref)),
            }
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
            revalidator=PlatformStateRevalidator(
                FakePlatformReaderPort(
                    state_hash_by_entity={
                        str(campaign_ref): confirmed_state_hash,
                        str(ad_set_ref): confirmed_state_hash,
                    }
                )
            ),
            platform_write=_ScriptedPlatformWrite(
                created_external_id=_EXTERNAL_ID, confirmed_state_hash=confirmed_state_hash
            ),
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
            platform=_FakeUploadPlatform(
                handle=PlatformAssetHandle(
                    platform_asset_id=_IMAGE_HASH,
                    preview_url="https://graph.facebook.com/v20.0/preview.png",
                )
            ),
            creative_bytes=_FakeCreativeBytes(
                {str(self.package.ad_sets[0].ads[0].creative.asset_id): _MEDIA}
            ),
            signer=FakeSignerPort(),
            clock=clock,
        )
        return RunPackagePublication(
            packages=self.packages,
            publications=self.publications,
            steps=self.steps,
            step_executor=executor,
            brakes=self.brakes,
            entity_registration=RegisterCreatedEntity(SqlAdEntityRepository(self.session)),
            entity_activation_refresh=RefreshRegisteredEntityState(
                SqlAdEntityRepository(self.session)
            ),
            clock=clock,
        )


async def test_a_package_with_a_real_ad_set_and_ad_reaches_published(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    owner_id = await owner_factory.create()
    business_id = await business_factory.create()
    scope = await seed_package_scope(db_session, owner_id=owner_id, business_id=business_id)
    scenario = Scenario(db_session, scope)
    publication_id = await scenario.approve()
    scenario.clock.advance_to(_CANCEL_GRACE_ELAPSED)

    run = scenario.use_case()
    result = None
    for _ in range(5):
        result = await run.execute(publication_id)
        await db_session.flush()

    assert result is not None
    assert result.publication_state == "completed"
    assert result.package_state == PackageState.PUBLISHED.value
    for step_index in range(5):
        record = await scenario.steps.get(publication_id, step_index)
        assert record is not None
        assert record.state == "done"

    # T123/AL-4: el conjunto y el anuncio heredaron su `expected_state_hash`
    # del recibo confirmado del padre -- lo prueba que la publicacion entera
    # llego a `completed` (antes de este cierre, el primer CREATE_AD_SET
    # fallaba siempre en `AdChildCreationError`) y que cada paso archivo su
    # propio recibo.
    ad_set_step = await scenario.steps.get(publication_id, _AD_SET_STEP)
    ad_step = await scenario.steps.get(publication_id, _AD_STEP)
    assert ad_set_step is not None
    assert ad_set_step.confirmed_state_hash == _CONFIRMED_STATE_HASH
    assert ad_step is not None
    assert ad_step.confirmed_state_hash == _CONFIRMED_STATE_HASH


async def _run_until_create_ad_set_then_halt_on_brake(
    scenario: Scenario, run: RunPackagePublication, publication_id: str, db_session: AsyncSession
) -> None:
    # UPLOAD_CREATIVE, CREATE_CAMPAIGN, CREATE_AD_SET: la campaña y el
    # conjunto quedan registrados PAUSED, nada activo todavia.
    for _ in range(3):
        await run.execute(publication_id)
        await db_session.flush()
    for step_index in range(3):
        record = await scenario.steps.get(publication_id, step_index)
        assert record is not None
        assert record.state == "done"

    # El dueño pulsa "Parar cambios" justo antes de CREATE_AD.
    scenario.brakes.engage_now(
        EmergencyBrake(
            scope=BrakeScope(kind=BrakeScopeKind.GLOBAL), mode=BrakeMode.AUTONOMOUS, engaged=True
        )
    )
    halted = await run.execute(publication_id)
    await db_session.flush()
    assert halted.publication_state == "halted"
    assert halted.package_state == PackageState.PARTIALLY_PUBLISHED.value


async def _release_brake_and_resume(scenario: Scenario, db_session: AsyncSession) -> None:
    # El dueño suelta el freno y pulsa "Continuar": `ResumePackagePublication`
    # solo transiciona la fila (`halted` -> `running`) -- el propio
    # `RunPackagePublication` retoma por `cursor`, sin duplicar nada.
    scenario.brakes.engage_now(
        EmergencyBrake(
            scope=BrakeScope(kind=BrakeScopeKind.GLOBAL), mode=BrakeMode.AUTONOMOUS, engaged=False
        )
    )
    resume = ResumePackagePublication(
        packages=scenario.packages, publications=scenario.publications, clock=scenario.clock
    )
    await resume.execute(
        ResumePackagePublicationCommand(
            business_id=scenario.package.business_id,
            package_id=scenario.package.package_id,
            package_hash=scenario.package.package_hash.value,
            resumed_by="owner-1",
        )
    )
    await db_session.flush()


def _real_entity_refs(scenario: Scenario) -> tuple[EntityRef, EntityRef, EntityRef]:
    campaign_ref = _campaign_ref(scenario.package, _EXTERNAL_ID)
    ad_set_ref = _ad_set_ref(scenario.package, _EXTERNAL_ID)
    ad_ref = EntityRef(
        platform=scenario.package.account_ref.platform,
        level=EntityLevel.AD,
        external_id=_EXTERNAL_ID,
        business_id=scenario.package.business_id.value,
        connection_id=scenario.package.account_ref.connection_id,
    )
    return campaign_ref, ad_set_ref, ad_ref


async def _assert_ad_entities_after_activation(
    entities: SqlAdEntityRepository, scenario: Scenario
) -> EntityRef:
    """Item 1 + item 2 (repaso 0.2.23) contra Postgres real: la campaña
    nacio PAUSED y `ACTIVATE_CAMPAIGN` la subio a ACTIVE con el hash
    confirmado; el conjunto y el anuncio se quedan PAUSED -- ningun paso
    los activa individualmente -- con la cadena de padres intacta."""
    campaign_ref, ad_set_ref, ad_ref = _real_entity_refs(scenario)

    campaign_entity = await entities.get_by_ref(campaign_ref)
    assert campaign_entity is not None
    assert campaign_entity.status is AdEntityStatus.ACTIVE
    assert campaign_entity.platform_state_hash.value == _ACTIVATED_STATE_HASH
    assert campaign_entity.parent_ref == scenario.package.account_ref

    ad_set_entity = await entities.get_by_ref(ad_set_ref)
    assert ad_set_entity is not None
    assert ad_set_entity.status is AdEntityStatus.PAUSED
    assert ad_set_entity.parent_ref == campaign_ref

    ad_entity = await entities.get_by_ref(ad_ref)
    assert ad_entity is not None
    assert ad_entity.status is AdEntityStatus.PAUSED
    assert ad_entity.parent_ref == ad_set_ref
    return campaign_ref


async def _assert_no_drift_after_activation(
    entities: SqlAdEntityRepository, campaign_ref: EntityRef, scenario: Scenario
) -> None:
    # Item 2: una campaña recien publicada no aparece DRIFTED -- la lectura
    # remota coincide con el hash que `ACTIVATE_CAMPAIGN` acaba de refrescar,
    # nunca con el de su creacion (PAUSED).
    drift = DetectPlatformDrift(
        FakeAdsPlatformPort(
            entity_states={
                campaign_ref: EntityStateSnapshot(
                    entity_ref=campaign_ref,
                    status=AdEntityStatus.ACTIVE,
                    is_controllable=True,
                    canonical_state=_ACTIVATED_CANONICAL_STATE,
                    fetched_at=scenario.clock.now(),
                )
            }
        ),
        entities,
        FakeEventBus(),
        scenario.clock,
    )
    assert await drift.execute(campaign_ref) is False
    reloaded_campaign = await entities.get_by_ref(campaign_ref)
    assert reloaded_campaign is not None
    assert reloaded_campaign.status is AdEntityStatus.ACTIVE


async def test_halts_after_create_ad_set_resumes_and_activates_against_real_ad_entities(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    """Item 3 (repaso 0.2.23): gap de cobertura -- de las pruebas de
    `packages/integration`, solo esta suite golpea `AdEntityRepository` real
    (0027/0035); ninguna cubria un halt A MITAD de saga + reanudacion sobre
    filas reales de `ad_entities`. Cierra items 1 y 2 contra Postgres real:
    la campaña/conjunto/anuncio nacen PAUSED (item 1) y `ACTIVATE_CAMPAIGN`
    sube la campaña a ACTIVE con el hash confirmado (item 2), sin que
    `DetectPlatformDrift` la marque DRIFTED nada mas publicarse."""
    owner_id = await owner_factory.create()
    business_id = await business_factory.create()
    scope = await seed_package_scope(db_session, owner_id=owner_id, business_id=business_id)
    scenario = Scenario(db_session, scope)
    publication_id = await scenario.approve()
    scenario.clock.advance_to(_CANCEL_GRACE_ELAPSED)
    run = scenario.use_case(confirmed_state_hash=_ACTIVATED_STATE_HASH)

    await _run_until_create_ad_set_then_halt_on_brake(scenario, run, publication_id, db_session)
    await _release_brake_and_resume(scenario, db_session)

    result = None
    for _ in range(2):  # CREATE_AD, ACTIVATE_CAMPAIGN
        result = await run.execute(publication_id)
        await db_session.flush()

    assert result is not None
    assert result.publication_state == "completed"
    assert result.package_state == PackageState.PUBLISHED.value

    entities = SqlAdEntityRepository(db_session)
    campaign_ref = await _assert_ad_entities_after_activation(entities, scenario)
    await _assert_no_drift_after_activation(entities, campaign_ref, scenario)
