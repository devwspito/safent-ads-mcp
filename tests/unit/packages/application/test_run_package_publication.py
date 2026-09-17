"""`RunPackagePublication` (T023): un paso por invocacion, R3 (huella viva),
AL-1 (freno), R5 (padre/creativo SOLO desde su propio recibo confirmado) y
AL-3 (el desenlace se relee siempre de lo que persistio el ejecutor, nunca
de un valor en memoria)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from safent_ads.accounts.application.errors import AdEntityParentNotFoundError
from safent_ads.accounts.application.refresh_registered_entity_state import (
    RefreshRegisteredEntityState,
)
from safent_ads.accounts.application.register_created_entity import RegisterCreatedEntity
from safent_ads.accounts.domain.ad_entity import AdEntity, AdEntityStatus
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
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.testing.fakes import FakeSignerPort
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

from ..domain.conftest import (
    google_campaign,
    performance_max_ad_set,
    performance_max_campaign_native,
    propose_google_package,
    propose_meta_package,
)
from .conftest import (
    FakeCampaignPackageRepository,
    FakePackageAuthorizationRepository,
    FakePackagePublicationRepository,
    FakePackageStepExecutorPort,
    FakePackageStepRepository,
)
from .test_approve_campaign_package import _wide_open_guardrails

_SEARCH_ONLY = frozenset({GoogleAdvertisingChannelType.SEARCH})

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)


class Scenario:
    """Un paquete Meta de 1 conjunto x 1 anuncio, ya aprobado de verdad
    (via `ApproveCampaignPackage`, con las mismas dependencias en memoria)
    -- el plan de pasos real es [UPLOAD_CREATIVE, CREATE_CAMPAIGN,
    CREATE_AD_SET, CREATE_AD, ACTIVATE_CAMPAIGN]."""

    def __init__(
        self,
        *,
        outcomes: dict[int, StepExecutionOutcome] | None = None,
        package: CampaignPackage | None = None,
    ) -> None:
        self.package = package or propose_meta_package(now=NOW, ttl_hours=72)
        self.packages = FakeCampaignPackageRepository()
        self.packages.by_id[str(self.package.package_id)] = self.package
        self.publications = FakePackagePublicationRepository()
        self.steps = FakePackageStepRepository()
        self.brakes = FakeBrakeStatePort()
        self.clock = FixedClock(NOW)
        self.executor = FakePackageStepExecutorPort(outcomes or {})
        self.publication_id = ""

    async def approve(
        self,
        *,
        enabled_google_channels: frozenset[GoogleAdvertisingChannelType] = _SEARCH_ONLY,
    ) -> None:
        await self.approve_without_advancing_the_clock(
            enabled_google_channels=enabled_google_channels
        )
        # Pasada la gracia previa de 45 s (FR-08): estos escenarios prueban
        # la EJECUCION del paso a paso, no la ventana de cancelacion (que
        # tiene su propia clase de pruebas mas abajo).
        self.clock.advance_to(NOW + timedelta(seconds=46))

    async def approve_without_advancing_the_clock(
        self,
        *,
        enabled_google_channels: frozenset[GoogleAdvertisingChannelType] = _SEARCH_ONLY,
    ) -> None:
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
            enabled_google_channels=enabled_google_channels,
        )
        result = await approve.execute(
            ApproveCampaignPackageCommand(
                business_id=self.package.business_id,
                package_id=self.package.package_id,
                package_hash=self.package.package_hash.value,
                approved_by="owner-1",
            )
        )
        self.publication_id = result.publication_id

    def use_case(
        self,
        *,
        enabled_google_channels: frozenset[GoogleAdvertisingChannelType] = _SEARCH_ONLY,
    ) -> RunPackagePublication:
        entities = InMemoryAdEntityRepository()
        return RunPackagePublication(
            packages=self.packages,
            publications=self.publications,
            steps=self.steps,
            step_executor=self.executor,
            brakes=self.brakes,
            entity_registration=RegisterCreatedEntity(entities),
            entity_activation_refresh=RefreshRegisteredEntityState(entities),
            clock=self.clock,
            enabled_google_channels=enabled_google_channels,
        )


async def _scenario(outcomes: dict[int, StepExecutionOutcome] | None = None) -> Scenario:
    scenario = Scenario(outcomes=outcomes)
    await scenario.approve()
    return scenario


def _done(
    created_entity_ref: str | None = None, *, confirmed_state_hash: str | None = None
) -> StepExecutionOutcome:
    return StepExecutionOutcome(
        state="done",
        created_entity_ref=created_entity_ref,
        outcome_code=None,
        confirmed_state_hash=confirmed_state_hash,
    )


def _scoped_campaign_ref(scenario: Scenario, external_id: str = "123456") -> EntityRef:
    """`RegisterCreatedEntity` exige que `entity_ref` y `parent_ref` viajen
    con el MISMO alcance (`AdEntity.__post_init__`) -- el mismo que
    `ChokepointStepExecutor._created_entity_ref` produce en produccion,
    nunca la forma corta sin alcance que el resto de esta suite usa para lo
    que no toca `ad_entities`."""
    return EntityRef(
        platform=scenario.package.account_ref.platform,
        level=EntityLevel.CAMPAIGN,
        external_id=external_id,
        business_id=scenario.package.business_id.value,
        connection_id=scenario.package.account_ref.connection_id,
    )


class _FailingAdEntityRepository:
    """`AdEntityRepository` cuyo `save` siempre rechaza -- simula un padre
    que no resuelve en `ad_entities`/`platform_accounts` (0035), el fallo
    real que `RunPackagePublication._register_created_entity` debe parar
    con una razon limpia en vez de dejar pasar la saga."""

    async def get_by_ref(self, entity_ref: EntityRef) -> AdEntity | None:
        del entity_ref
        return None

    async def save(self, entity: AdEntity) -> None:
        raise AdEntityParentNotFoundError(f"{entity.entity_ref} declara un padre inexistente")

    async def save_many(self, entities: object) -> None:  # pragma: no cover - no usado aqui
        del entities


class TestHappyPathRunsOneStepPerInvocation:
    async def test_five_steps_end_with_the_package_published(self) -> None:
        scenario = await _scenario(
            {
                0: _done("meta-image-hash-abc"),  # UPLOAD_CREATIVE
                1: _done("meta:campaign:123456"),  # CREATE_CAMPAIGN
                2: _done("meta:ad_set:as-1"),  # CREATE_AD_SET
                3: _done("meta:ad:ad-1"),  # CREATE_AD
                4: _done(None),  # ACTIVATE_CAMPAIGN
            }
        )
        use_case = scenario.use_case()

        for _ in range(5):
            result = await use_case.execute(scenario.publication_id)

        assert result.publication_state == "completed"
        assert result.package_state == PackageState.PUBLISHED.value
        published = scenario.packages.by_id[str(scenario.package.package_id)]
        assert published.state is PackageState.PUBLISHED

    async def test_create_ad_set_receives_the_campaigns_confirmed_entity_ref_as_parent(
        self,
    ) -> None:
        scenario = await _scenario(
            {
                0: _done(None),
                1: _done("meta:campaign:123456"),
                2: _done("meta:ad_set:as-1"),
                3: _done("meta:ad:ad-1"),
                4: _done(None),
            }
        )
        use_case = scenario.use_case()
        for _ in range(3):
            await use_case.execute(scenario.publication_id)

        ad_set_binding = scenario.executor.calls[-1]
        assert ad_set_binding.step_kind.value == "CREATE_AD_SET"
        assert ad_set_binding.parent_entity_ref is not None
        assert ad_set_binding.parent_entity_ref.external_id == "123456"

    async def test_create_ad_step_only_runs_once_its_upload_receipt_is_done(self) -> None:
        scenario = await _scenario(
            {
                0: _done("meta-image-hash-abc"),
                1: _done("meta:campaign:123456"),
                2: _done("meta:ad_set:as-1"),
                3: _done("meta:ad:ad-1"),
                4: _done(None),
            }
        )
        use_case = scenario.use_case()
        for _ in range(4):
            await use_case.execute(scenario.publication_id)

        record = await scenario.steps.get(scenario.publication_id, 3)
        assert record is not None
        assert record.state == "done"


class TestParentMustBeConfirmedByItsOwnReceipt:
    async def test_ad_set_step_does_not_run_if_the_campaign_receipt_is_tampered_with(self) -> None:
        scenario = await _scenario({0: _done(None), 1: _done("meta:campaign:123456")})
        use_case = scenario.use_case()
        await use_case.execute(scenario.publication_id)  # UPLOAD_CREATIVE
        await use_case.execute(scenario.publication_id)  # CREATE_CAMPAIGN

        # Manipular el recibo del padre en la base -- exactamente el ataque
        # que R5 cierra: nunca se confia en otra cosa que no sea el propio
        # recibo confirmado.
        campaign_record = await scenario.steps.get(scenario.publication_id, 1)
        assert campaign_record is not None
        tampered = replace(campaign_record, created_entity_ref=None, state="running")
        await scenario.steps.upsert(tampered)

        result = await use_case.execute(scenario.publication_id)

        assert result.publication_state == "halted"
        assert len(scenario.executor.calls) == 2  # el CREATE_AD_SET nunca se ejecuto


class TestBrakeStopsBetweenSteps:
    async def test_brake_engaged_halts_without_running_the_next_step(self) -> None:
        scenario = await _scenario({0: _done(None), 1: _done("meta:campaign:123456")})
        use_case = scenario.use_case()
        await use_case.execute(scenario.publication_id)
        scenario.brakes.engage_now(
            EmergencyBrake(
                scope=BrakeScope(kind=BrakeScopeKind.GLOBAL),
                mode=BrakeMode.AUTONOMOUS,
                engaged=True,
            )
        )

        result = await use_case.execute(scenario.publication_id)

        assert result.publication_state == "halted"
        assert len(scenario.executor.calls) == 1
        # M6 (revision de codigo): un halt PRE-FLIGHT no corresponde a
        # ningun paso -- `failed_step_index` es `None`, nunca el cursor
        # (que solo diria "en que paso ocurrio", y aqui no ocurrio en
        # ninguno todavia).
        record = await scenario.publications.get_by_id(scenario.publication_id)
        assert record is not None
        assert record.failed_step_index is None


class TestPartialFailureLeavesNothingActive:
    async def test_failure_on_the_third_step_marks_the_package_partially_published(self) -> None:
        scenario = await _scenario(
            {
                0: _done(None),
                1: _done("meta:campaign:123456"),
                2: StepExecutionOutcome(
                    state="failed", created_entity_ref=None, outcome_code="ad_set_creation_failed"
                ),
            }
        )
        use_case = scenario.use_case()
        result = None
        for _ in range(3):
            result = await use_case.execute(scenario.publication_id)

        assert result is not None
        assert result.publication_state == "halted"
        assert result.package_state == PackageState.PARTIALLY_PUBLISHED.value
        record = await scenario.publications.get_by_id(scenario.publication_id)
        assert record is not None
        assert record.failed_step_index == 2


class TestPackageChangedMidPublicationHaltsBeforeExecuting:
    async def test_hash_mismatch_halts_without_calling_the_executor(self) -> None:
        scenario = await _scenario({0: _done(None)})
        scenario.package.budget = replace(scenario.package.budget, daily=Money.of("999.00"))

        result = await scenario.use_case().execute(scenario.publication_id)

        assert result.publication_state == "halted"
        assert scenario.executor.calls == []
        record = await scenario.publications.get_by_id(scenario.publication_id)
        assert record is not None
        assert record.failed_step_index is None


class TestApprovalExpiresBetweenSteps:
    """AL-6/T125 (revision de codigo): el broker ya deniega un paso con el
    sobre caducado, pero `RunPackagePublication` debe fallar ANTES de
    materializar la `Proposal`/`Authorization` derivada -- mismo codigo
    estable que `resume` (`package_approval_expired`)."""

    async def test_halts_without_calling_the_executor_once_the_envelope_expired(self) -> None:
        scenario = await _scenario({0: _done(None)})
        scenario.clock.advance_to(NOW + timedelta(minutes=31))

        result = await scenario.use_case().execute(scenario.publication_id)

        assert result.publication_state == "halted"
        assert scenario.executor.calls == []
        record = await scenario.publications.get_by_id(scenario.publication_id)
        assert record is not None
        assert record.halt_reason == "package_approval_expired"
        assert record.failed_step_index is None


class TestHaltAfterVerifyingNeverRaises:
    """B1 (revision de codigo, 15-sep): `CampaignPackage._TRANSITIONS`
    prohibe VERIFYING -> FAILED a proposito (BL-4/INV-4: un `unknown` nunca
    se reconcilia afirmando "no se creo nada"). Antes del arreglo, un halt
    con `created_count == 0` tras pasar por VERIFYING elegia
    `NoneCreatedOutcome` y `CampaignPackage.record_publication_outcome`
    lanzaba `CampaignPackageInvariantError` -- que escapaba de
    `RunPackagePublication.execute()` sin capturar (regresion: mataba el
    bucle entero del worker, no solo esta publicacion)."""

    async def test_unknown_then_permanently_failed_ends_partially_published_not_raising(
        self,
    ) -> None:
        unknown_outcome = StepExecutionOutcome(
            state="unknown", created_entity_ref=None, outcome_code=None
        )
        scenario = await _scenario({0: unknown_outcome})
        use_case = scenario.use_case()

        first = await use_case.execute(scenario.publication_id)
        assert first.publication_state == "running"
        assert first.package_state == PackageState.VERIFYING.value

        scenario.executor._outcomes[0] = StepExecutionOutcome(
            state="failed", created_entity_ref=None, outcome_code="upload_permanently_failed"
        )

        second = await use_case.execute(scenario.publication_id)

        assert second.publication_state == "halted"
        assert second.package_state == PackageState.PARTIALLY_PUBLISHED.value


class TestRegistersCreatedEntities:
    """003-entidades-creadas: `proposals_entity_exists()` (0027) exige que
    la campaña que la MISMA saga crea viva ya en `ad_entities` antes de que
    su hijo (`CREATE_AD_SET`/`CREATE_AD`/`ACTIVATE_CAMPAIGN`) pueda
    proponerse -- `RunPackagePublication` la registra en cuanto su recibo
    confirma, antes de devolver el control."""

    def _run_case(self, scenario: Scenario, entities: object) -> RunPackagePublication:
        return RunPackagePublication(
            packages=scenario.packages,
            publications=scenario.publications,
            steps=scenario.steps,
            step_executor=scenario.executor,
            brakes=scenario.brakes,
            entity_registration=RegisterCreatedEntity(entities),  # type: ignore[arg-type]
            entity_activation_refresh=RefreshRegisteredEntityState(  # type: ignore[arg-type]
                entities
            ),
            clock=scenario.clock,
        )

    async def test_registers_the_campaign_once_its_receipt_confirms(self) -> None:
        entities = InMemoryAdEntityRepository()
        scenario = await _scenario({0: _done(None), 1: _done(None)})
        campaign_ref = _scoped_campaign_ref(scenario)
        scenario.executor._outcomes[1] = _done(str(campaign_ref), confirmed_state_hash="a" * 64)
        use_case = self._run_case(scenario, entities)
        await use_case.execute(scenario.publication_id)  # UPLOAD_CREATIVE

        result = await use_case.execute(scenario.publication_id)  # CREATE_CAMPAIGN

        assert result.publication_state == "running"
        stored = await entities.get_by_ref(campaign_ref)
        assert stored is not None
        assert stored.platform_state_hash.value == "a" * 64
        assert stored.parent_ref == scenario.package.account_ref
        # Item 1 (repaso 0.2.23): la plataforma nunca crea una entidad ya
        # activa -- `platform_completeness` firma "PAUSED" para los tres
        # pasos que crean; `ACTIVATE_CAMPAIGN` la sube a ACTIVE despues.
        assert stored.status is AdEntityStatus.PAUSED

    async def test_skips_registration_when_the_receipt_has_no_confirmed_hash(self) -> None:
        # Mismo criterio de tolerancia que `_resolve_parent` (T123/AL-4): un
        # `done` sin `confirmed_state_hash` no falla aqui.
        entities = InMemoryAdEntityRepository()
        scenario = await _scenario({0: _done(None), 1: _done("meta:campaign:123456")})
        use_case = self._run_case(scenario, entities)
        await use_case.execute(scenario.publication_id)

        result = await use_case.execute(scenario.publication_id)

        assert result.publication_state == "running"
        stored = await entities.get_by_ref(
            EntityRef(PlatformCode.META, EntityLevel.CAMPAIGN, "123456")
        )
        assert stored is None

    async def test_halts_cleanly_when_the_parent_does_not_resolve(self) -> None:
        scenario = await _scenario({0: _done(None), 1: _done(None)})
        campaign_ref = _scoped_campaign_ref(scenario)
        scenario.executor._outcomes[1] = _done(str(campaign_ref), confirmed_state_hash="a" * 64)
        use_case = self._run_case(scenario, _FailingAdEntityRepository())
        await use_case.execute(scenario.publication_id)  # UPLOAD_CREATIVE

        result = await use_case.execute(scenario.publication_id)  # CREATE_CAMPAIGN

        assert result.publication_state == "halted"
        record = await scenario.publications.get_by_id(scenario.publication_id)
        assert record is not None
        assert record.halt_reason == "package_entity_registration_parent_missing"
        # La escritura en la plataforma fue real -- el paso SI queda `done`,
        # la saga para DESPUES, nunca finge que la campaña no se creo.
        campaign_step = await scenario.steps.get(scenario.publication_id, 1)
        assert campaign_step is not None
        assert campaign_step.state == "done"


class TestRefreshesActivatedCampaign:
    """Item 2 (repaso 0.2.23): `ACTIVATE_CAMPAIGN` MODIFICA la campaña que
    su propio `CREATE_CAMPAIGN` ya registro PAUSED -- sin esto,
    `DetectPlatformDrift` la marcaria DRIFTED nada mas publicarse (compara
    contra el hash de creacion, nunca actualizado) y el siguiente ciclo
    firmaria un `expected_state_hash` obsoleto."""

    def _run_case(self, scenario: Scenario, entities: object) -> RunPackagePublication:
        return RunPackagePublication(
            packages=scenario.packages,
            publications=scenario.publications,
            steps=scenario.steps,
            step_executor=scenario.executor,
            brakes=scenario.brakes,
            entity_registration=RegisterCreatedEntity(entities),  # type: ignore[arg-type]
            entity_activation_refresh=RefreshRegisteredEntityState(  # type: ignore[arg-type]
                entities
            ),
            clock=scenario.clock,
        )

    async def test_flips_the_campaign_to_active_with_the_confirmed_hash(self) -> None:
        entities = InMemoryAdEntityRepository()
        scenario = await _scenario(
            {
                0: _done("meta-image-hash-abc"),
                1: _done(None),
                2: _done("meta:ad_set:as-1"),
                3: _done("meta:ad:ad-1"),
                4: _done(None),
            }
        )
        campaign_ref = _scoped_campaign_ref(scenario)
        scenario.executor._outcomes[1] = _done(str(campaign_ref), confirmed_state_hash="a" * 64)
        scenario.executor._outcomes[4] = _done(None, confirmed_state_hash="b" * 64)
        use_case = self._run_case(scenario, entities)

        result = None
        for _ in range(5):
            result = await use_case.execute(scenario.publication_id)

        assert result is not None
        assert result.publication_state == "completed"
        stored = await entities.get_by_ref(campaign_ref)
        assert stored is not None
        assert stored.status is AdEntityStatus.ACTIVE
        assert stored.platform_state_hash.value == "b" * 64

    async def test_halts_cleanly_when_the_campaign_was_never_registered(self) -> None:
        # Mismo criterio de tolerancia que el registro: si `CREATE_CAMPAIGN`
        # nunca archivo un `confirmed_state_hash` (recibo previo a la
        # columna 0049, T123/AL-4), el registro se omite -- `ACTIVATE_
        # CAMPAIGN` no puede refrescar una fila que no existe, para la
        # saga en vez de fingir que la activacion se reflejo.
        entities = InMemoryAdEntityRepository()
        scenario = await _scenario(
            {
                0: _done("meta-image-hash-abc"),
                1: _done("meta:campaign:123456"),
                2: _done("meta:ad_set:as-1"),
                3: _done("meta:ad:ad-1"),
                4: _done(None, confirmed_state_hash="b" * 64),
            }
        )
        use_case = self._run_case(scenario, entities)

        for _ in range(4):
            await use_case.execute(scenario.publication_id)
        result = await use_case.execute(scenario.publication_id)  # ACTIVATE_CAMPAIGN

        assert result.publication_state == "halted"
        record = await scenario.publications.get_by_id(scenario.publication_id)
        assert record is not None
        assert record.halt_reason == "package_entity_activation_refresh_missing"


class TestCancelGraceWindow:
    """FR-08: dentro de los 45 s previos, nada se escribe -- ni siquiera el
    primer paso arranca, para que "Deshacer" pueda cancelar sin haber
    tocado nada."""

    async def test_nothing_runs_within_the_45_second_grace_window(self) -> None:
        scenario = Scenario(outcomes={0: _done("meta-image-hash-abc")})
        await scenario.approve_without_advancing_the_clock()

        result = await scenario.use_case().execute(scenario.publication_id)

        assert result.publication_state == "pending"
        assert result.package_state == PackageState.APPROVED.value
        assert scenario.executor.calls == []


class TestChannelDisabledBetweenApprovalAndPublication:
    """T035 security re-check (2026-09-15, CWE-284): `ApproveCampaignPackage`
    already gates this at approval time, but `ADS_GOOGLE_CHANNELS_ENABLED`
    can narrow between approving and a worker tick that runs the
    publication -- PRE-FLIGHT, before any step is attempted, so nothing new
    is ever written for a channel this installation stopped allowing."""

    async def test_a_performance_max_publication_halts_before_the_first_step(self) -> None:
        package = propose_google_package(
            campaign=google_campaign(native=performance_max_campaign_native()),
            ad_sets=(performance_max_ad_set(),),
            now=NOW,
            ttl_hours=72,
        )
        scenario = Scenario(outcomes={0: _done("google:asset:logo-1")}, package=package)
        pmax_enabled = frozenset(
            {GoogleAdvertisingChannelType.SEARCH, GoogleAdvertisingChannelType.PERFORMANCE_MAX}
        )
        await scenario.approve(enabled_google_channels=pmax_enabled)

        # `use_case()` defaults to SEARCH-only -- the installation's setting
        # narrowed after `approve()` already ran above.
        result = await scenario.use_case().execute(scenario.publication_id)

        assert result.publication_state == "halted"
        assert scenario.executor.calls == []
        record = await scenario.publications.get_by_id(scenario.publication_id)
        assert record is not None
        assert record.halt_reason == "package_channel_not_enabled"
        assert record.failed_step_index is None

    async def test_a_performance_max_publication_still_runs_once_the_channel_stays_enabled(
        self,
    ) -> None:
        package = propose_google_package(
            campaign=google_campaign(native=performance_max_campaign_native()),
            ad_sets=(performance_max_ad_set(),),
            now=NOW,
            ttl_hours=72,
        )
        scenario = Scenario(outcomes={0: _done("google:asset:logo-1")}, package=package)
        pmax_enabled = frozenset(
            {GoogleAdvertisingChannelType.SEARCH, GoogleAdvertisingChannelType.PERFORMANCE_MAX}
        )
        await scenario.approve(enabled_google_channels=pmax_enabled)

        result = await scenario.use_case(enabled_google_channels=pmax_enabled).execute(
            scenario.publication_id
        )

        assert result.publication_state == "running"
        assert len(scenario.executor.calls) == 1
        assert scenario.executor.calls[0].step_index == 0
