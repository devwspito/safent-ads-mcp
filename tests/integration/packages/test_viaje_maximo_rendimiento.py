"""T040 (tasks.md): viaje de aceptacion de
Maximo Rendimiento de punta a punta -- `RunPackagePublication` +
`ChokepointStepExecutor` REALES contra Postgres real, mismo patron que
`test_publicacion_completa_ejecutor_real.py` (Meta), para un paquete
`PERFORMANCE_MAX` de un solo grupo de recursos con tres imagenes.

Alcance declarado, igual que ese fichero declara el suyo: la plataforma
(`_FakePmaxUploadPlatform`/`_ScriptedPlatformWrite`) es un doble en la
frontera `AdsPlatformPort`/`WritePort` -- esta prueba ejercita la SAGA de
paquetes de extremo a extremo (aprobacion -> paso a paso -> activacion),
no el SDK de Google Ads ni el bróker por socket
(`WriteAuthorizationPipeline`/`GoogleAdsAdapter`/`native_ad_child.
google_create`), que T031-T034 ya prueban por su cuenta
(`tests/unit/broker/platforms/test_google_ads_adapter.py`,
`test_google_asset_group_create.py`, `test_native_ad_child.py`). Lo que SI
demuestra, y que antes de este carril era imposible (gap cerrado en
`platform_completeness._google_asset_group_wire`): el grupo de recursos
firmado lleva `AssetGroup.name` y un hueco `{creative_of:X}` por imagen, y
ese hueco resuelve al manejador de plataforma REAL que devolvio su propio
`UPLOAD_CREATIVE` -- nunca al objeto de imagen crudo ni al de otra
imagen."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from sqlalchemy import text
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
from safent_ads.execution.domain.execution_attempt import build_package_step_idempotency_key
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
from safent_ads.packages.domain.approval_envelope import (
    StepKind,
    creative_hole,
    derive_step_plan,
    image_local_ref,
)
from safent_ads.packages.domain.campaign_package import CampaignPackage, PackageState
from safent_ads.packages.domain.identifiers import OfferingId
from safent_ads.packages.domain.planned_tree import GoogleAssetGroupNative, PlannedAdSet
from safent_ads.packages.domain.platform_completeness import ad_set_wire_plan
from safent_ads.packages.domain.values import LandingUrl
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
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType
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
    asset_group_assets,
    google_campaign,
    image_creative,
    performance_max_ad_set,
    performance_max_campaign_native,
    propose_google_package,
)
from tests.unit.packages.infrastructure.test_chokepoint_step_executor import (
    _ad_set_ref,
    _campaign_ref,
    _FakeCreativeBytes,
    _ScriptedPlatformWrite,
)

pytestmark = pytest.mark.integration

# T035 security re-check (CWE-284): this journey is specifically about
# PERFORMANCE_MAX (the channel-enablement gate defaults to `SEARCH`-only) --
# the gate itself has its own coverage
# (`tests/unit/packages/application/test_approve_campaign_package.py`,
# `tests/unit/packages/application/test_run_package_publication.py`,
# `tests/unit/execution/test_chokepoint.py`).
_PMAX_ENABLED = frozenset(
    {GoogleAdvertisingChannelType.SEARCH, GoogleAdvertisingChannelType.PERFORMANCE_MAX}
)
_CANCEL_GRACE_ELAPSED = NOW + timedelta(seconds=46)
_LOGO_MEDIA = b"tiny-fake-pmax-logo-bytes"
_MARKETING_MEDIA = b"tiny-fake-pmax-marketing-bytes"
_SQUARE_MEDIA = b"tiny-fake-pmax-square-bytes"
_LOGO_CHECKSUM = hashlib.sha256(_LOGO_MEDIA).hexdigest()
_MARKETING_CHECKSUM = hashlib.sha256(_MARKETING_MEDIA).hexdigest()
_SQUARE_CHECKSUM = hashlib.sha256(_SQUARE_MEDIA).hexdigest()
_EXTERNAL_ID = "555"
_CONFIRMED_STATE_HASH = hashlib.sha256(b"pmax-journey-confirmed-state").hexdigest()
_LOGO_ASSET_RESOURCE = "customers/9990000001/assets/111"
_MARKETING_ASSET_RESOURCE = "customers/9990000001/assets/112"
_SQUARE_ASSET_RESOURCE = "customers/9990000001/assets/113"
# 3 x UPLOAD_CREATIVE + CREATE_CAMPAIGN + CREATE_AD_SET + ACTIVATE_CAMPAIGN --
# nunca CREATE_AD (T024/casilla 15: el grupo de recursos ES el anuncio).
_TOTAL_STEPS = 6


@dataclass
class _FakePmaxUploadPlatform:
    """`AdsPlatformPort` doble propio de este viaje: a diferencia de
    `_FakeUploadPlatform` (un unico manejador fijo, suficiente para el
    viaje de Meta con una sola imagen), Maximo Rendimiento sube TRES
    imagenes distintas bajo la MISMA aprobacion -- este doble devuelve un
    manejador de plataforma DISTINTO por `asset_id` (`request.file_name`,
    `chokepoint_step_executor._execute_upload_creative` lo rellena asi),
    para poder demostrar que cada hueco `{creative_of:X}` del grupo de
    recursos resuelve al recurso que LE TOCABA, nunca al de otra imagen."""

    handles_by_asset_id: dict[str, PlatformAssetHandle]
    calls: list[AssetUploadRequest] = field(default_factory=list)

    async def upload_asset(self, request: AssetUploadRequest) -> PlatformAssetHandle:
        self.calls.append(request)
        return self.handles_by_asset_id[request.file_name]


def _pmax_ad_set() -> PlannedAdSet:
    return performance_max_ad_set(
        native=GoogleAssetGroupNative(
            final_url=LandingUrl("https://clinicax.example/reservar"),
            assets=asset_group_assets(
                logo=image_creative(checksum=_LOGO_CHECKSUM, width=1080, height=1080),
                marketing_image=image_creative(
                    checksum=_MARKETING_CHECKSUM, width=1200, height=628
                ),
                square_image=image_creative(checksum=_SQUARE_CHECKSUM, width=1080, height=1080),
            ),
        )
    )


def _pmax_package(scope: SeededScope | None = None) -> CampaignPackage:
    scope_kwargs: dict[str, object] = (
        {}
        if scope is None
        else {
            "business": scope.business_id,
            "account": scope.account_ref,
            "offering_id": OfferingId(scope.offering_id),
        }
    )
    return propose_google_package(
        campaign=google_campaign(native=performance_max_campaign_native()),
        ad_sets=(_pmax_ad_set(),),
        **scope_kwargs,
    )


class Scenario:
    def __init__(self, session: AsyncSession, scope: SeededScope) -> None:
        self.session = session
        self.package = _pmax_package(scope)
        self.packages = SqlCampaignPackageRepository(session)
        self.publications = SqlPackagePublicationRepository(session)
        self.steps = SqlPackageStepRepository(session)
        self.brakes = FakeBrakeStatePort()
        self.clock = FixedClock(NOW)
        self.execution_queue = FakeExecutionQueuePort()
        assets = self.package.ad_sets[0].native.assets
        self.upload_platform = _FakePmaxUploadPlatform(
            handles_by_asset_id={
                str(assets.logo.asset_id): PlatformAssetHandle(
                    platform_asset_id=_LOGO_ASSET_RESOURCE, preview_url=None
                ),
                str(assets.marketing_image.asset_id): PlatformAssetHandle(
                    platform_asset_id=_MARKETING_ASSET_RESOURCE, preview_url=None
                ),
                str(assets.square_image.asset_id): PlatformAssetHandle(
                    platform_asset_id=_SQUARE_ASSET_RESOURCE, preview_url=None
                ),
            }
        )
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
            enabled_google_channels=_PMAX_ENABLED,
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
        assets = self.package.ad_sets[0].native.assets
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
            enabled_google_channels=_PMAX_ENABLED,
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
            creative_bytes=_FakeCreativeBytes(
                {
                    str(assets.logo.asset_id): _LOGO_MEDIA,
                    str(assets.marketing_image.asset_id): _MARKETING_MEDIA,
                    str(assets.square_image.asset_id): _SQUARE_MEDIA,
                }
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
            enabled_google_channels=_PMAX_ENABLED,
        )


async def _scenario(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> Scenario:
    owner_id = await owner_factory.create()
    business_id = await business_factory.create()
    scope = await seed_package_scope(
        db_session, owner_id=owner_id, business_id=business_id, platform=PlatformCode.GOOGLE
    )
    return Scenario(db_session, scope)


async def test_el_viaje_de_maximo_rendimiento_llega_a_published(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    scenario = await _scenario(db_session, owner_factory, business_factory)
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

    records = [await scenario.steps.get(publication_id, index) for index in range(_TOTAL_STEPS)]
    assert all(record is not None and record.state == "done" for record in records)

    upload_kind = StepKind.UPLOAD_CREATIVE.value.lower()
    upload_records = [r for r in records if r is not None and r.kind == upload_kind]
    assert len(upload_records) == 3
    assert len({record.local_ref for record in upload_records}) == 3

    campaign_ref = _campaign_ref(scenario.package, _EXTERNAL_ID)
    entities = SqlAdEntityRepository(db_session)
    campaign_entity = await entities.get_by_ref(campaign_ref)
    assert campaign_entity is not None
    assert campaign_entity.status is AdEntityStatus.ACTIVE


def test_el_grupo_de_recursos_no_tiene_ningun_paso_create_ad_y_depende_de_sus_tres_imagenes() -> (
    None
):
    """Casilla 15/T043: Maximo Rendimiento es el propio grupo de recursos
    -- `derive_step_plan` (el MISMO que firma `ApproveCampaignPackage`) no
    produce ni un solo `CREATE_AD`, y su `CREATE_AD_SET` depende de sus
    tres imagenes."""
    package = _pmax_package()

    plan = derive_step_plan(package)

    assert not any(step.step_kind is StepKind.CREATE_AD for step in plan)
    create_ad_set = next(step for step in plan if step.step_kind is StepKind.CREATE_AD_SET)
    assets = package.ad_sets[0].native.assets
    expected_holes = {
        image_local_ref(assets.logo.checksum),
        image_local_ref(assets.marketing_image.checksum),
        image_local_ref(assets.square_image.checksum),
    }
    assert set(create_ad_set.depends_on) == expected_holes
    assert plan[-1].step_kind is StepKind.ACTIVATE_CAMPAIGN
    assert plan[-1].expected_done_steps == 1


async def test_cada_imagen_resuelve_al_manejador_que_le_toca_en_el_grupo_de_recursos(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    """Prueba directa del arreglo de `platform_completeness.
    _google_asset_group_wire`: sin `AssetGroup.name` ni los tres huecos
    `{creative_of:X}`, el `WriteCommand` que llega a la plataforma para
    `CREATE_AD_SET` llevaria el objeto de imagen crudo (checksum/mime/
    dimensiones) en vez del `resource_name` que cada imagen recibio al
    subirse -- y nunca el nombre del grupo."""
    scenario = await _scenario(db_session, owner_factory, business_factory)
    publication_id = await scenario.approve()
    scenario.clock.advance_to(_CANCEL_GRACE_ELAPSED)
    run = scenario.use_case()

    for _ in range(5):  # 3 x UPLOAD_CREATIVE + CREATE_CAMPAIGN + CREATE_AD_SET
        await run.execute(publication_id)
        await db_session.flush()

    create_ad_set_calls = [
        call
        for call in scenario.platform_write.calls
        if "child_plan" in call.value
        and call.value["child_plan"]["native"]["kind"] == "ASSET_GROUP"
    ]
    assert len(create_ad_set_calls) == 1
    native_assets = create_ad_set_calls[0].value["child_plan"]["native"]["assets"]

    assert native_assets["name"] == scenario.package.ad_sets[0].name
    assert native_assets["logo"] == _LOGO_ASSET_RESOURCE
    assert native_assets["marketing_image"] == _MARKETING_ASSET_RESOURCE
    assert native_assets["square_image"] == _SQUARE_ASSET_RESOURCE


async def test_cada_paso_archiva_su_recibo_bajo_la_clave_pkg_publicacion_paso(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    scenario = await _scenario(db_session, owner_factory, business_factory)
    publication_id = await scenario.approve()
    scenario.clock.advance_to(_CANCEL_GRACE_ELAPSED)
    run = scenario.use_case()

    for _ in range(_TOTAL_STEPS):
        await run.execute(publication_id)
        await db_session.flush()

    saved_keys = {attempt.idempotency_key for attempt in scenario.execution_queue.saved.values()}
    # Los tres UPLOAD_CREATIVE (pasos 0-2) no pasan por el chokepoint (BL-6):
    # solo CREATE_CAMPAIGN/CREATE_AD_SET/ACTIVATE_CAMPAIGN (3, 4, 5) archivan
    # su intento bajo esta clave.
    for step_index in range(3, _TOTAL_STEPS):
        assert build_package_step_idempotency_key(publication_id, step_index) in saved_keys


async def test_con_el_grupo_de_recursos_pendiente_la_activacion_no_corre(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    """Casilla 15/T043: aunque el cursor de `campaign_package_publications`
    llegara a `ACTIVATE_CAMPAIGN` (una reanudacion inconsistente, o un
    cursor manipulado fuera de esta saga -- la unica forma de simularlo sin
    un doble del repositorio), `_structure_complete_before_activation`
    recuenta contra el sobre FIRMADO: hallar el `CREATE_AD_SET` sin
    `state='done'` basta para que `RunPackagePublication` HALTE en vez de
    activar. La campaña se queda `PAUSED`, nunca `ACTIVE`."""
    scenario = await _scenario(db_session, owner_factory, business_factory)
    publication_id = await scenario.approve()
    scenario.clock.advance_to(_CANCEL_GRACE_ELAPSED)
    run = scenario.use_case()

    for _ in range(4):  # 3 x UPLOAD_CREATIVE + CREATE_CAMPAIGN -- CREATE_AD_SET queda pendiente
        await run.execute(publication_id)
        await db_session.flush()
    ad_set_step = await scenario.steps.get(publication_id, 4)
    assert ad_set_step is None or ad_set_step.state != "done"

    await db_session.execute(
        text("UPDATE campaign_package_publications SET cursor = 5 WHERE id = :publication_id"),
        {"publication_id": publication_id},
    )
    await db_session.flush()

    result = await run.execute(publication_id)
    await db_session.flush()

    assert result.publication_state == "halted"
    assert result.package_state == PackageState.PARTIALLY_PUBLISHED.value

    campaign_ref = _campaign_ref(scenario.package, _EXTERNAL_ID)
    entities = SqlAdEntityRepository(db_session)
    campaign_entity = await entities.get_by_ref(campaign_ref)
    assert campaign_entity is not None
    assert campaign_entity.status is AdEntityStatus.PAUSED


def test_el_hueco_de_cada_imagen_del_sobre_firmado_es_el_que_resuelve_el_ejecutor() -> None:
    """Ancla el formato exacto que `platform_completeness._asset_group_
    image_hole` duplica a proposito (no importa `creative_hole`/
    `image_local_ref` de `approval_envelope`, evitaria un ciclo): si un
    lado cambia de formato sin el otro, esta prueba lo detecta antes que
    un viaje de punta a punta mas caro."""
    ad_set = _pmax_ad_set()

    wire = ad_set_wire_plan(ad_set, PlatformCode.GOOGLE)

    assets = ad_set.native.assets
    native_assets = wire["native"]["assets"]
    assert native_assets["logo"] == creative_hole(image_local_ref(assets.logo.checksum))
    assert native_assets["marketing_image"] == creative_hole(
        image_local_ref(assets.marketing_image.checksum)
    )
    assert native_assets["square_image"] == creative_hole(
        image_local_ref(assets.square_image.checksum)
    )
