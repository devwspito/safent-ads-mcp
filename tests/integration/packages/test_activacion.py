"""T123 (AL-4): la activacion solo se proyecta cuando TODOS los pasos
previos estan `done` -- en particular, `#CREATE_AD done == #PlannedAd
firmados` (`expected_done_steps`). Un fallo en cualquier anuncio detiene la
saga antes de llegar a `ACTIVATE_CAMPAIGN`: la campaña queda creada, pero
en pausa, nunca activa con menos anuncios de los que el dueño aprobo."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text
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
from safent_ads.packages.domain.planned_tree import GoogleAssetGroupNative
from safent_ads.packages.domain.step_binding import PackageStepBinding
from safent_ads.packages.domain.values import LandingUrl
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
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType
from safent_ads.proposals.testing.fakes import FakeSignerPort
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
    meta_ad,
    meta_ad_set,
    performance_max_ad_set,
    performance_max_campaign_native,
    propose_google_package,
    propose_meta_package,
)

pytestmark = pytest.mark.integration

# T035 security re-check (CWE-284): `test_pmax_con_un_grupo_de_recursos_
# pendiente_no_activa` is specifically about PERFORMANCE_MAX -- the
# channel-enablement gate defaults to `SEARCH`-only and has its own coverage
# elsewhere (`tests/unit/packages/application/test_approve_campaign_package.py`,
# `tests/unit/packages/application/test_run_package_publication.py`).
_PMAX_ENABLED = frozenset(
    {GoogleAdvertisingChannelType.SEARCH, GoogleAdvertisingChannelType.PERFORMANCE_MAX}
)
_CANCEL_GRACE_ELAPSED = NOW + timedelta(seconds=46)

# Plan de pasos de un paquete de 1 conjunto x 4 anuncios (misma imagen, un
# solo UPLOAD_CREATIVE): 0=UPLOAD, 1=CAMPAIGN, 2=AD_SET, 3..6=AD1..AD4,
# 7=ACTIVATE (expected_done_steps=4).
_UPLOAD_STEP = 0
_CAMPAIGN_STEP = 1
_AD_SET_STEP = 2
_AD4_STEP = 6
_ACTIVATE_STEP = 7


def _four_ad_package(scope: SeededScope) -> CampaignPackage:
    ads = tuple(meta_ad(local_ref=f"as#1/ad#{i}") for i in range(1, 5))
    return propose_meta_package(
        business=scope.business_id,
        account=scope.account_ref,
        offering_id=OfferingId(scope.offering_id),
        ad_sets=(meta_ad_set(ads=ads),),
    )


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
    def __init__(
        self,
        session: AsyncSession,
        scope: SeededScope,
        *,
        package: CampaignPackage | None = None,
        enabled_google_channels: frozenset[GoogleAdvertisingChannelType] = frozenset(
            {GoogleAdvertisingChannelType.SEARCH}
        ),
    ) -> None:
        self.session = session
        self.scope = scope
        self.package = package if package is not None else _four_ad_package(scope)
        self.packages = SqlCampaignPackageRepository(session)
        self.publications = SqlPackagePublicationRepository(session)
        self.steps = SqlPackageStepRepository(session)
        self.brakes = FakeBrakeStatePort()
        self.clock = FixedClock(NOW)
        self.enabled_google_channels = enabled_google_channels

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
            enabled_google_channels=self.enabled_google_channels,
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
            enabled_google_channels=self.enabled_google_channels,
        )
        return run_publication, executor


async def test_fallo_en_el_anuncio_4_deja_la_campana_en_pausa(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    owner_id = await owner_factory.create()
    business_id = await business_factory.create()
    scope = await seed_package_scope(db_session, owner_id=owner_id, business_id=business_id)
    scenario = Scenario(db_session, scope)
    publication_id = await scenario.approve()
    scenario.clock.advance_to(_CANCEL_GRACE_ELAPSED)

    run, executor = scenario.use_case(
        {
            _UPLOAD_STEP: _done("meta-image-hash-abc"),
            _CAMPAIGN_STEP: _done("meta:campaign:123456"),
            _AD_SET_STEP: _done("meta:ad_set:as-1"),
            3: _done("meta:ad:ad-1"),
            4: _done("meta:ad:ad-2"),
            5: _done("meta:ad:ad-3"),
            _AD4_STEP: StepExecutionOutcome(
                state="failed", created_entity_ref=None, outcome_code="ad_rejected_by_policy"
            ),
        }
    )

    result = None
    for _ in range(_AD4_STEP + 1):
        result = await run.execute(publication_id)

    assert result is not None
    assert result.publication_state == "halted"
    assert result.package_state == PackageState.PARTIALLY_PUBLISHED.value
    # ACTIVATE_CAMPAIGN (indice 7) jamas se intento: el anuncio 4 fallo
    # justo antes, y expected_done_steps=4 nunca se cumplio.
    assert _ACTIVATE_STEP not in executor.calls
    assert await scenario.steps.get(publication_id, _ACTIVATE_STEP) is None

    reloaded_publication = await scenario.publications.get_by_id(publication_id)
    assert reloaded_publication is not None
    assert reloaded_publication.state == "halted"
    assert reloaded_publication.halt_reason == "ad_rejected_by_policy"

    # La campaña y el conjunto SI se crearon -- quedan en pausa, no
    # desaparecen -- pero ningun anuncio 4 ni la activacion.
    campaign_step = await scenario.steps.get(publication_id, _CAMPAIGN_STEP)
    assert campaign_step is not None
    assert campaign_step.state == "done"
    assert campaign_step.created_entity_ref == "meta:campaign:123456"
    failed_ad_step = await scenario.steps.get(publication_id, _AD4_STEP)
    assert failed_ad_step is not None
    assert failed_ad_step.state == "failed"
    assert failed_ad_step.created_entity_ref is None


# Plan de pasos de un paquete de Maximo Rendimiento con DOS grupos de
# recursos (checksums todos distintos, 3 imagenes cada uno): 0..5=UPLOAD,
# 6=CAMPAIGN, 7=AD_SET(as#1), 8=AD_SET(as#2), 9=ACTIVATE
# (expected_done_steps=2, T043).
_PMAX_CAMPAIGN_STEP = 6
_PMAX_AD_SET_1_STEP = 7
_PMAX_AD_SET_2_STEP = 8
_PMAX_ACTIVATE_STEP = 9


def _two_asset_group_package(scope: SeededScope) -> CampaignPackage:
    ad_set_two = performance_max_ad_set(
        local_ref="as#2",
        native=GoogleAssetGroupNative(
            final_url=LandingUrl("https://clinicax.example/reservar"),
            assets=asset_group_assets(
                logo=image_creative(checksum="e" * 64, width=1080, height=1080),
                marketing_image=image_creative(checksum="f" * 64, width=1200, height=628),
                square_image=image_creative(checksum="g" * 64, width=1080, height=1080),
            ),
        ),
    )
    return propose_google_package(
        business=scope.business_id,
        account=scope.account_ref,
        offering_id=OfferingId(scope.offering_id),
        campaign=google_campaign(native=performance_max_campaign_native()),
        ad_sets=(performance_max_ad_set(), ad_set_two),
    )


async def test_pmax_con_un_grupo_de_recursos_pendiente_no_activa(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    """T043/BL-3: `_resolve_parent` de `ACTIVATE_CAMPAIGN` solo reverifica
    su PADRE directo (la campaña) -- nunca a sus hermanos. Con el cursor de
    la publicacion adelantado hasta la activacion mientras el SEGUNDO grupo
    de recursos sigue `pending` (nunca ejecutado), solo la comprobacion de
    `expected_done_steps` de esta tarea lo detiene antes de encender el
    gasto."""
    owner_id = await owner_factory.create()
    business_id = await business_factory.create()
    scope = await seed_package_scope(
        db_session, owner_id=owner_id, business_id=business_id, platform=PlatformCode.GOOGLE
    )
    package = _two_asset_group_package(scope)
    scenario = Scenario(db_session, scope, package=package, enabled_google_channels=_PMAX_ENABLED)
    publication_id = await scenario.approve()
    scenario.clock.advance_to(_CANCEL_GRACE_ELAPSED)

    run, executor = scenario.use_case(
        {
            0: _done("google:asset:logo-1"),
            1: _done("google:asset:marketing-1"),
            2: _done("google:asset:square-1"),
            3: _done("google:asset:logo-2"),
            4: _done("google:asset:marketing-2"),
            5: _done("google:asset:square-2"),
            _PMAX_CAMPAIGN_STEP: _done("google:campaign:123456"),
            _PMAX_AD_SET_1_STEP: _done("google:ad_group:as-1"),
            _PMAX_ACTIVATE_STEP: _done(None),
        }
    )

    for _ in range(_PMAX_AD_SET_1_STEP + 1):
        await run.execute(publication_id)

    # El segundo grupo de recursos (indice 8) nunca se ejecuto -- sigue sin
    # fila en `campaign_package_steps` (nace `pending` solo al alcanzarlo).
    assert await scenario.steps.get(publication_id, _PMAX_AD_SET_2_STEP) is None

    # Adelanta el cursor directamente en la base, sin pasar por el paso 8:
    # la corrupcion/race que esta tarea asume como amenaza, nunca producida
    # por `RunPackagePublication` en un ciclo normal (avanza de uno en uno).
    await db_session.execute(
        text("UPDATE campaign_package_publications SET cursor = :cursor WHERE id = :id"),
        {"cursor": _PMAX_ACTIVATE_STEP, "id": publication_id},
    )
    await db_session.flush()

    result = await run.execute(publication_id)

    assert result.publication_state == "halted"
    assert result.package_state == PackageState.PARTIALLY_PUBLISHED.value
    assert _PMAX_ACTIVATE_STEP not in executor.calls
    assert await scenario.steps.get(publication_id, _PMAX_ACTIVATE_STEP) is None

    reloaded_publication = await scenario.publications.get_by_id(publication_id)
    assert reloaded_publication is not None
    assert reloaded_publication.halt_reason == "structure_incomplete_before_activation"
