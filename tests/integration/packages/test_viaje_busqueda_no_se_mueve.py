"""T041 (tasks.md): Busqueda no se ha
movido. Dos pruebas complementarias:

1. `TestPackageHashCongeladoEnT003NoCambia` -- reimporta las constantes
   congeladas de `test_search_canonical_unchanged.py` (T003) y vuelve a
   comprobarlas desde este carril: si el arreglo del grupo de recursos de
   Maximo Rendimiento (`platform_completeness._google_asset_group_wire`)
   hubiera tocado una sola linea de `_google_ad_group_wire`/`campaign_
   wire_plan` (las funciones que SI comparte con Busqueda), esta prueba lo
   detectaria por su cuenta, sin depender de que nadie recuerde correr
   T003.

2. `test_el_viaje_de_busqueda_llega_a_published_igual_que_antes` -- el
   MISMO patron de "ejecutor real" que `test_publicacion_completa_
   ejecutor_real.py` (Meta) y `test_viaje_maximo_rendimiento.py` (T040,
   Maximo Rendimiento), aqui para un grupo de anuncios de Busqueda
   (`GoogleAdGroupNative` + un `PlannedAd` de texto puro) -- ninguna
   prueba de integracion existente hacia pasar un paquete de Busqueda de
   Google por `RunPackagePublication`/`ChokepointStepExecutor` reales
   antes de este carril. Cero pasos `UPLOAD_CREATIVE` (RSA es texto puro,
   T003 lo congela como `creative:{"kind":"text_only"}`): la plataforma de
   subida nunca deberia recibir una llamada, y esta prueba lo hace
   explicito con un doble que revienta si le llega una.

Nota de alcance (paralela a la de T040): el `package_hash` de la prueba 1
es el mismo `_FROZEN_PACKAGE_HASH` de T003 porque usa las MISMAS
identidades congeladas (no toca Postgres); la prueba 2 siembra un negocio
real y por tanto un `business_id`/`account_ref` propios -- el `package_
hash` de ESE paquete es distinto por construccion (identidad de cuenta
distinta), pero su FORMA DE CABLE (`campaign_wire_plan`/`ad_set_wire_plan`/
`ad_wire_plan`) es exactamente la misma funcion, sobre la misma entrada,
que T003 ya fija -- por eso la prueba 2 tambien compara esa forma de cable
operacion por operacion contra lo que esas funciones producen en frio,
en vez de un hash que la identidad de cuenta cambiaria de todos modos."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.application.ports import AssetUploadRequest, PlatformAssetHandle
from safent_ads.accounts.application.refresh_registered_entity_state import (
    RefreshRegisteredEntityState,
)
from safent_ads.accounts.application.register_created_entity import RegisterCreatedEntity
from safent_ads.accounts.domain.ad_entity import AdEntityStatus
from safent_ads.accounts.infrastructure.sql_repositories import SqlAdEntityRepository
from safent_ads.execution.application.chokepoint import ExecutionChokepoint
from safent_ads.execution.application.platform_state_revalidator import PlatformStateRevalidator
from safent_ads.execution.domain.guardrails import GuardrailEvaluator
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
from safent_ads.packages.application.run_package_publication import RunPackagePublication
from safent_ads.packages.domain.campaign_package import CampaignPackage, PackageState
from safent_ads.packages.domain.identifiers import OfferingId
from safent_ads.packages.domain.package_hash import compute_package_hash
from safent_ads.packages.domain.platform_completeness import ad_set_wire_plan, campaign_wire_plan
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
from safent_ads.proposals.domain.diff_hash import canonical_json_bytes
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import ExpiryPolicy
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.proposals.testing.fakes import (
    FakeAuthorizationRepository,
    FakeSignerPort,
    FakeVerifierPort,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import PlatformCode
from tests.conftest import BusinessFactory, OwnerFactory
from tests.integration.packages.conftest import SeededScope, seed_package_scope
from tests.unit.packages.application.test_approve_campaign_package import _wide_open_guardrails
from tests.unit.packages.domain.conftest import (
    NOW,
    google_ad_set,
    google_campaign,
    propose_google_package,
)
from tests.unit.packages.domain.test_search_canonical_unchanged import (
    _FROZEN_CANONICAL_JSON,
    _FROZEN_PACKAGE_HASH,
    _search_payload,
)
from tests.unit.packages.infrastructure.test_chokepoint_step_executor import (
    _ad_set_ref,
    _campaign_ref,
    _FakeCreativeBytes,
    _ScriptedPlatformWrite,
)

pytestmark = pytest.mark.integration

_CANCEL_GRACE_ELAPSED = NOW + timedelta(seconds=46)
_EXTERNAL_ID = "777"
_CONFIRMED_STATE_HASH = "c" * 64
# CREATE_CAMPAIGN + CREATE_AD_SET + CREATE_AD + ACTIVATE_CAMPAIGN, cero
# UPLOAD_CREATIVE: el RSA de Busqueda es texto puro (T003).
_TOTAL_STEPS = 4


class TestPackageHashCongeladoEnT003NoCambia:
    """Reafirma T003 desde este carril (casillas 3 y 24 del modelo de
    amenaza): el fix de `_google_asset_group_wire` no toca ni una linea de
    `_google_ad_group_wire`/`campaign_wire_plan`."""

    def test_el_hash_congelado_de_busqueda_no_cambia(self) -> None:
        assert compute_package_hash(_search_payload()).value == _FROZEN_PACKAGE_HASH

    def test_la_canonica_json_congelada_de_busqueda_no_cambia(self) -> None:
        assert canonical_json_bytes(_search_payload()).decode() == _FROZEN_CANONICAL_JSON


@dataclass
class _UnreachableUploadPlatform:
    """Un RSA de Busqueda es texto puro (T003: `creative:{"kind":
    "text_only"}`) -- `derive_step_plan` no produce ni un `UPLOAD_CREATIVE`
    para este paquete, asi que `upload_asset` nunca deberia llamarse.
    Revienta si algo cambia eso sin que esta prueba se entere."""

    calls: list[AssetUploadRequest] = field(default_factory=list)

    async def upload_asset(self, request: AssetUploadRequest) -> PlatformAssetHandle:
        raise AssertionError(f"upload_asset no deberia llamarse para Busqueda: {request!r}")


def _search_package(scope: SeededScope) -> CampaignPackage:
    return propose_google_package(
        business=scope.business_id,
        account=scope.account_ref,
        offering_id=OfferingId(scope.offering_id),
        campaign=google_campaign(),
        ad_sets=(google_ad_set(),),
    )


class Scenario:
    def __init__(self, session: AsyncSession, scope: SeededScope) -> None:
        self.session = session
        self.package = _search_package(scope)
        self.packages = SqlCampaignPackageRepository(session)
        self.publications = SqlPackagePublicationRepository(session)
        self.steps = SqlPackageStepRepository(session)
        self.brakes = FakeBrakeStatePort()
        self.clock = FixedClock(NOW)
        self.execution_queue = FakeExecutionQueuePort()
        self.upload_platform = _UnreachableUploadPlatform()
        self.platform_write = _ScriptedPlatformWrite(
            created_external_id=_EXTERNAL_ID, confirmed_state_hash=_CONFIRMED_STATE_HASH
        )

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

    def use_case(self) -> RunPackagePublication:
        clock = self.clock
        campaign_ref = _campaign_ref(self.package, _EXTERNAL_ID)
        ad_set_ref = _ad_set_ref(self.package, _EXTERNAL_ID)
        account_scope = str(self.package.account_ref)
        proposals = SqlProposalRepository(self.session)
        authorizations = FakeAuthorizationRepository()
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
            queue=self.execution_queue,
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
                        str(campaign_ref): _CONFIRMED_STATE_HASH,
                        str(ad_set_ref): _CONFIRMED_STATE_HASH,
                    }
                )
            ),
            platform_write=self.platform_write,
            recorder=FakeDecisionRecorder(),
            clock=clock,
        )
        executor = ChokepointStepExecutor(
            proposals=proposals,
            authorizations=authorizations,
            propose_action=propose_action,
            execution_queue=self.execution_queue,
            chokepoint=chokepoint,
            guardrail_evaluator=GuardrailEvaluator(),
            guardrail_sets=guardrail_sets,
            spend_ledger=spend_ledger,
            platform=self.upload_platform,
            creative_bytes=_FakeCreativeBytes({}),
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


async def test_el_viaje_de_busqueda_llega_a_published_igual_que_antes(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    owner_id = await owner_factory.create()
    business_id = await business_factory.create()
    scope = await seed_package_scope(
        db_session, owner_id=owner_id, business_id=business_id, platform=PlatformCode.GOOGLE
    )
    scenario = Scenario(db_session, scope)
    publication_id = await scenario.approve()
    scenario.clock.advance_to(_CANCEL_GRACE_ELAPSED)
    run = scenario.use_case()

    result = None
    for _ in range(_TOTAL_STEPS):
        result = await run.execute(publication_id)
        await db_session.flush()

    assert result is not None
    assert result.publication_state == "completed"
    assert result.package_state == PackageState.PUBLISHED.value
    assert scenario.upload_platform.calls == []

    campaign, ad_set = scenario.package.campaign, scenario.package.ad_sets[0]
    expected_campaign_wire = {"creation_plan": campaign_wire_plan(campaign)}
    expected_ad_set_wire = {"child_plan": ad_set_wire_plan(ad_set, PlatformCode.GOOGLE)}

    calls_by_shape = [call.value for call in scenario.platform_write.calls]
    assert expected_campaign_wire in calls_by_shape
    assert expected_ad_set_wire in calls_by_shape

    entities = SqlAdEntityRepository(db_session)
    campaign_entity = await entities.get_by_ref(_campaign_ref(scenario.package, _EXTERNAL_ID))
    assert campaign_entity is not None
    assert campaign_entity.status is AdEntityStatus.ACTIVE


def test_package_tree_payload_de_busqueda_sigue_produciendo_el_arbol_congelado() -> None:
    """Puente explicito entre el `package_hash` congelado (T003) y la
    forma de cable que ambos viajes (T040 y este) comparten
    (`campaign_wire_plan`): si `package_tree_payload` divergiera de
    `campaign_wire_plan` para el mismo `PlannedCampaign`, Busqueda podria
    seguir firmando el mismo `package_hash` mientras publica algo
    distinto."""
    payload = _search_payload()

    assert payload["plan"]["campaign"]["native"] == campaign_wire_plan(google_campaign())["native"]
