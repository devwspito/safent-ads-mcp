"""Tests directos de `ChokepointStepExecutor.execute_step` (H4).

`tests/unit/packages/application/test_run_package_publication.py` ya cubre
la saga completa contra un `PackageStepExecutorPort` doble -- nunca ejerce
la implementacion REAL. Aqui se monta el mismo camino de escritura que
`ads-worker` usaria de verdad (`ProposeAction` + `ExecutionChokepoint`, con
dobles en memoria solo en los puertos de I/O: plataforma, bytes de
creatividad, firma) y se llama a `execute_step` directamente, un paso a la
vez, derivando cada `PackageStepBinding` del MISMO sobre firmado que
`ApproveCampaignPackage` produce."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.application.ports import AssetUploadRequest, PlatformAssetHandle
from safent_ads.execution.application.chokepoint import ExecutionChokepoint
from safent_ads.execution.application.platform_state_revalidator import PlatformStateRevalidator
from safent_ads.execution.application.ports import WriteCommand, WriteResult
from safent_ads.execution.domain.execution_attempt import build_package_step_idempotency_key
from safent_ads.execution.domain.guardrails import GuardrailEvaluator, GuardrailSet
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
from safent_ads.packages.domain.approval_envelope import (
    PackageApprovalEnvelope,
    creative_hole,
    image_local_ref,
)
from safent_ads.packages.domain.campaign_package import CampaignPackage
from safent_ads.packages.domain.planned_tree import GoogleAssetGroupNative
from safent_ads.packages.domain.step_binding import PackageStepBinding, derive_step_binding
from safent_ads.packages.domain.values import LandingUrl
from safent_ads.packages.infrastructure.chokepoint_step_executor import (
    ChokepointStepExecutor,
    _find_asset_id_by_checksum,
)
from safent_ads.proposals.application.propose_action import ProposeAction
from safent_ads.proposals.domain.authorization import AuthorizationVerifier
from safent_ads.proposals.domain.classification import ClassificationPolicy
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import ExpiryPolicy
from safent_ads.proposals.testing.fakes import (
    FakeAuthorizationRepository,
    FakeProposalRepository,
    FakeSignerPort,
    FakeVerifierPort,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef

from ..application.conftest import (
    FakeCampaignPackageRepository,
    FakePackageAuthorizationRepository,
    FakePackagePublicationRepository,
)
from ..application.test_approve_campaign_package import _wide_open_guardrails
from ..domain.conftest import (
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

NOW = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
# T035 security re-check (CWE-284): this module exercises the step executor
# across every Google channel, `PERFORMANCE_MAX` included (`performance_max_
# campaign_native`) -- the channel-enablement gate is a different concern
# (`tests/unit/packages/application/test_approve_campaign_package.py`,
# `tests/unit/execution/test_chokepoint.py`), so every fixture here enables
# all four rows rather than defaulting to `SEARCH`-only.
_ALL_GOOGLE_CHANNELS = frozenset(GoogleAdvertisingChannelType)
_MEDIA = b"tiny-fake-png-bytes"
_CHECKSUM = hashlib.sha256(_MEDIA).hexdigest()
_UPLOAD_STEP = 0
_CAMPAIGN_STEP = 1
_AD_SET_STEP = 2
_AD_STEP = 3
_ACTIVATE_STEP = 4

# H4-1 (hallazgo de esta rama de QA, sin arreglar): `ExecutionAttempt.
# claim_for_package_step` fija `started_at=now` en el mismo instante en que
# `ChokepointStepExecutor._execute_write_step` va a pedirle al chokepoint
# que reclame ESE MISMO intento (`chokepoint.run_once(proposal_id=...)`
# inmediatamente despues de `execution_queue.save(...)`, sin ceder el
# control a otro ciclo). `SqlExecutionQueue.claim_next` (y el doble
# `FakeExecutionQueuePort` que lo modela) solo permite reclamar una fila
# `CLAIMED` cuando `started_at IS NULL` o ha superado `DEFAULT_CLAIM_LEASE`
# (5 min) -- un intento con `started_at=now` no cumple ninguna de las dos
# el mismo instante en que se crea. El `run_once` inmediato nunca encuentra
# nada que reclamar, `_to_step_outcome` traduce el intento -- todavia en
# `CLAIMED` -- al `else` por defecto (`state="failed"`,
# `outcome_code="step_failed"`). Contraste: `entity_lifecycle_actions.py`/
# `apply_defensive_action.py` (el mismo patron "proponer + ejecutar ya" para
# acciones sueltas) construyen su `ExecutionAttempt` SIN pasar `started_at`
# (queda `None` por defecto) precisamente para que el `run_once` inmediato
# pueda reclamarlo. Efecto en produccion: NINGUN paso de escritura de un
# paquete (`CREATE_CAMPAIGN`/`CREATE_AD_SET`/`CREATE_AD`/
# `ACTIVATE_CAMPAIGN`) puede terminar `done` en su primer intento -- solo
# `UPLOAD_CREATIVE` (que no pasa por el chokepoint) escapa de esto. Nunca
# detectado hasta ahora porque toda la cobertura existente de la saga
# (`tests/unit/packages/application/test_run_package_publication.py`,
# `tests/integration/packages/test_publication_saga.py`) sustituye
# `ChokepointStepExecutor` entero por un doble de `PackageStepExecutorPort`.
_H4_1_REASON = (
    "H4-1: ExecutionAttempt.claim_for_package_step fija started_at=now, y el "
    "run_once(proposal_id=...) inmediato de ChokepointStepExecutor nunca "
    "puede reclamar esa fila (SqlExecutionQueue.claim_next exige started_at "
    "IS NULL o vencido el lease de 5 min) -- todo paso de escritura de un "
    "paquete falla como step_failed en su primer intento."
)

# AL-4/T123 (gap cerrado, revision de codigo del 15-sep): H4-1 (arriba)
# tambien estaba cerrado -- pero `ad_child_creation.validate_child_diff`
# exigia `expected_state_hash` no vacio para CREATE_AD_SET/CREATE_AD
# (deteccion de deriva del padre) y `chokepoint_step_executor` nunca lo
# rellenaba: el padre lo crea la MISMA saga, no vive en `ad_entities`. Ahora
# `PackageStepBinding.parent_receipt_state_hash` (derivado por
# `RunPackagePublication` desde `PackageStepRecord.confirmed_state_hash`,
# columna 0049) alimenta `ProposeActionCommand.expected_state_hash` para
# CREATE_AD_SET/CREATE_AD/ACTIVATE_CAMPAIGN -- generalizacion de R2.8 (antes
# solo la activacion) a todo paso con padre. `Fixture.run(...,
# parent_confirmed_state_hash=...)` simula el recibo que
# `RunPackagePublication` ya habria archivado del paso padre.


def _package() -> CampaignPackage:
    return propose_meta_package(
        now=NOW, ad_sets=(meta_ad_set(ads=(meta_ad(checksum=_CHECKSUM),)),)
    )


def _campaign_ref(package: CampaignPackage, external_id: str = "123456") -> EntityRef:
    return EntityRef(
        platform=package.account_ref.platform,
        level=EntityLevel.CAMPAIGN,
        external_id=external_id,
        business_id=package.business_id.value,
        connection_id=package.account_ref.connection_id,
    )


def _ad_set_ref(package: CampaignPackage, external_id: str = "as-1") -> EntityRef:
    return EntityRef(
        platform=package.account_ref.platform,
        level=EntityLevel.AD_SET,
        external_id=external_id,
        business_id=package.business_id.value,
        connection_id=package.account_ref.connection_id,
    )


_PMAX_LOGO_MEDIA = b"tiny-fake-pmax-logo-bytes"
_PMAX_LOGO_CHECKSUM = hashlib.sha256(_PMAX_LOGO_MEDIA).hexdigest()


def _package_with_asset_group() -> CampaignPackage:
    ad_set = performance_max_ad_set(
        native=GoogleAssetGroupNative(
            final_url=LandingUrl("https://clinicax.example/reservar"),
            assets=asset_group_assets(
                logo=image_creative(checksum=_PMAX_LOGO_CHECKSUM, width=1080, height=1080)
            ),
        )
    )
    return propose_google_package(
        now=NOW,
        campaign=google_campaign(native=performance_max_campaign_native()),
        ad_sets=(ad_set,),
    )


class _FakeUploadPlatform:
    """Doble de `AdsPlatformPort`: solo implementa `upload_asset` --
    `ChokepointStepExecutor` nunca llama a ningun otro metodo del puerto
    para un paso `UPLOAD_CREATIVE`."""

    def __init__(
        self, *, handle: PlatformAssetHandle | None = None, error: Exception | None = None
    ) -> None:
        self._handle = handle or PlatformAssetHandle(
            platform_asset_id="meta-image-hash-abc",
            preview_url="https://graph.facebook.com/v20.0/preview.png",
        )
        self._error = error
        self.calls: list[AssetUploadRequest] = []

    async def upload_asset(self, request: AssetUploadRequest) -> PlatformAssetHandle:
        self.calls.append(request)
        if self._error is not None:
            raise self._error
        return self._handle


class _FakeCreativeBytes:
    def __init__(self, media_by_asset_id: dict[str, bytes]) -> None:
        self._media = media_by_asset_id

    async def get_bytes(self, *, business_id: BusinessId, asset_id: str) -> bytes | None:
        del business_id
        return self._media.get(asset_id)


class _ScriptedPlatformWrite:
    """Idempotente por `idempotency_key` (igual que `FakeAdsPlatformWritePort`
    de `execution.testing.fakes`), con `created_external_id` configurable --
    lo unico que ese doble no permite fijar, y que
    `ChokepointStepExecutor._created_entity_ref` necesita para escalar el
    `EntityRef` del recurso creado. `confirmed_state_hash` tambien es
    configurable (003-entidades-creadas: `RegisterCreatedEntity` exige un
    sha256 hexadecimal real -- el valor por defecto sigue siendo el opaco
    `"state-after"` para los tests que no registran nada en `ad_entities`)."""

    def __init__(
        self, *, created_external_id: str | None, confirmed_state_hash: str = "state-after"
    ) -> None:
        self._created_external_id = created_external_id
        self._confirmed_state_hash = confirmed_state_hash
        self._results: dict[str, WriteResult] = {}
        self.calls: list[WriteCommand] = []

    async def execute_write(
        self, command: WriteCommand, authorization: object, idempotency_key: str
    ) -> WriteResult:
        del authorization
        if idempotency_key in self._results:
            return self._results[idempotency_key]
        self.calls.append(command)
        result = WriteResult(
            applied_value=command.value,
            confirmed_state_hash=self._confirmed_state_hash,
            created_external_id=self._created_external_id,
        )
        self._results[idempotency_key] = result
        return result

    async def read_receipt(
        self, command: WriteCommand, authorization: object, idempotency_key: str
    ) -> WriteResult | None:
        del command, authorization
        return self._results.get(idempotency_key)


@dataclass
class Fixture:
    package: CampaignPackage
    envelope: PackageApprovalEnvelope
    envelope_hash: str
    human_authorization_id: str
    human_approval_signature: bytes
    execution_queue: FakeExecutionQueuePort
    platform: _FakeUploadPlatform
    platform_write: _ScriptedPlatformWrite
    executor: ChokepointStepExecutor

    def binding(
        self,
        step_index: int,
        parent_entity_ref: EntityRef | None,
        *,
        parent_confirmed_state_hash: str | None = None,
    ) -> PackageStepBinding:
        return derive_step_binding(
            self.envelope,
            self.envelope_hash,
            step_index,
            parent_entity_ref,
            parent_confirmed_state_hash,
        )

    async def run(
        self,
        step_index: int,
        *,
        parent_entity_ref: EntityRef | None = None,
        parent_confirmed_state_hash: str | None = None,
        resolutions: dict[str, str] | None = None,
        existing_proposal_id: str | None = None,
    ) -> object:
        return await self.executor.execute_step(
            package=self.package,
            envelope=self.envelope,
            binding=self.binding(
                step_index,
                parent_entity_ref,
                parent_confirmed_state_hash=parent_confirmed_state_hash,
            ),
            resolutions=resolutions or {},
            human_authorization_id=self.human_authorization_id,
            human_approval_signature=self.human_approval_signature,
            existing_proposal_id=existing_proposal_id,
        )


async def _approve(package: CampaignPackage) -> tuple[PackageApprovalEnvelope, str, str, bytes]:
    packages = FakeCampaignPackageRepository()
    packages.by_id[str(package.package_id)] = package
    publications = FakePackagePublicationRepository()
    account_scope = str(package.account_ref)
    approve = ApproveCampaignPackage(
        packages=packages,
        publications=publications,
        authorizations=FakePackageAuthorizationRepository(),
        brakes=FakeBrakeStatePort(),
        guardrail_evaluator=GuardrailEvaluator(),
        guardrail_sets=FakeGuardrailSetRepository(
            {account_scope: _wide_open_guardrails(account_scope)}
        ),
        spend_ledger=FakeSpendLedger(),
        signer=FakeSignerPort(),
        clock=FixedClock(NOW),
        enabled_google_channels=_ALL_GOOGLE_CHANNELS,
    )
    result = await approve.execute(
        ApproveCampaignPackageCommand(
            business_id=package.business_id,
            package_id=package.package_id,
            package_hash=package.package_hash.value,
            approved_by="owner-1",
        )
    )
    record = await publications.get_by_id(result.publication_id)
    assert record is not None
    return record.envelope, record.envelope_hash, record.authorization_id, record.approval_signature


async def _fixture(
    package: CampaignPackage,
    *,
    guardrails_by_scope: dict[str, GuardrailSet],
    platform: _FakeUploadPlatform | None = None,
    creative_bytes: _FakeCreativeBytes | None = None,
    created_external_id: str | None = "123456",
    platform_reader: FakePlatformReaderPort | None = None,
) -> Fixture:
    envelope, envelope_hash, human_authorization_id, human_approval_signature = await _approve(
        package
    )
    clock = FixedClock(NOW)
    proposals = FakeProposalRepository()
    authorizations = FakeAuthorizationRepository()
    execution_queue = FakeExecutionQueuePort()
    guardrail_sets = FakeGuardrailSetRepository(guardrails_by_scope)
    spend_ledger = FakeSpendLedger()
    platform_write = _ScriptedPlatformWrite(created_external_id=created_external_id)
    propose_action = ProposeAction(
        proposals=proposals,
        classification_policy=ClassificationPolicy(critical_impact_threshold=Money.of("999999")),
        expiry_policy=ExpiryPolicy(),
        clock=clock,
    )
    chokepoint = ExecutionChokepoint(
        reservations=FakeExecutionReservations(),
        queue=execution_queue,
        uow=FakeUnitOfWork(),
        brakes=FakeBrakeStatePort(),
        proposals=proposals,
        authorizations=authorizations,
        auth_verifier=AuthorizationVerifier(FakeVerifierPort(), clock),
        guardrail_evaluator=GuardrailEvaluator(),
        guardrail_sets=guardrail_sets,
        spend_ledger=spend_ledger,
        revalidator=PlatformStateRevalidator(platform_reader or FakePlatformReaderPort()),
        platform_write=platform_write,
        recorder=FakeDecisionRecorder(),
        clock=clock,
        enabled_google_channels=_ALL_GOOGLE_CHANNELS,
    )
    upload_platform = platform or _FakeUploadPlatform()
    executor = ChokepointStepExecutor(
        proposals=proposals,
        authorizations=authorizations,
        propose_action=propose_action,
        execution_queue=execution_queue,
        chokepoint=chokepoint,
        guardrail_evaluator=GuardrailEvaluator(),
        guardrail_sets=guardrail_sets,
        spend_ledger=spend_ledger,
        platform=upload_platform,
        creative_bytes=creative_bytes or _FakeCreativeBytes({}),
        signer=FakeSignerPort(),
        clock=clock,
    )
    return Fixture(
        package=package,
        envelope=envelope,
        envelope_hash=envelope_hash,
        human_authorization_id=human_authorization_id,
        human_approval_signature=human_approval_signature,
        execution_queue=execution_queue,
        platform=upload_platform,
        platform_write=platform_write,
        executor=executor,
    )


class TestHappyPathPerStepKind:
    async def test_upload_creative_returns_the_platforms_opaque_handle(self) -> None:
        """T112/BL-6 (revision de codigo): `{creative_of:X}` resuelve al
        manejador OPACO (`platform_asset_id`, el `image_hash` de Meta),
        nunca a `preview_url` -- eso es solo informativo, nunca lo que se
        firma ni lo que publica."""
        package = _package()
        asset_id = str(package.ad_sets[0].ads[0].creative.asset_id)
        fixture = await _fixture(
            package, guardrails_by_scope={}, creative_bytes=_FakeCreativeBytes({asset_id: _MEDIA})
        )

        outcome = await fixture.run(_UPLOAD_STEP)

        assert outcome.state == "done"
        assert outcome.created_entity_ref == "meta-image-hash-abc"

    async def test_create_campaign_scopes_the_created_external_id_into_an_entity_ref(self) -> None:
        package = _package()
        scope_ref = str(package.account_ref)
        fixture = await _fixture(
            package,
            guardrails_by_scope={scope_ref: _wide_open_guardrails(scope_ref)},
            created_external_id="123456",
        )

        outcome = await fixture.run(_CAMPAIGN_STEP)

        assert outcome.state == "done"
        assert outcome.created_entity_ref == str(_campaign_ref(package))

    async def test_create_ad_set_targets_exactly_the_supplied_parent(self) -> None:
        package = _package()
        parent = _campaign_ref(package)
        scope_ref = str(parent)
        fixture = await _fixture(
            package,
            guardrails_by_scope={scope_ref: _wide_open_guardrails(scope_ref)},
            created_external_id="as-1",
            platform_reader=FakePlatformReaderPort(
                state_hash_by_entity={str(parent): "state-after"}
            ),
        )

        outcome = await fixture.run(
            _AD_SET_STEP, parent_entity_ref=parent, parent_confirmed_state_hash="state-after"
        )

        assert outcome.state == "done"
        assert fixture.platform_write.calls[0].entity_ref == parent

    async def test_create_ad_resolves_the_creative_hole_into_the_uploaded_handle(self) -> None:
        package = _package()
        parent = _ad_set_ref(package)
        scope_ref = str(parent)
        fixture = await _fixture(
            package,
            guardrails_by_scope={scope_ref: _wide_open_guardrails(scope_ref)},
            created_external_id="ad-1",
            platform_reader=FakePlatformReaderPort(
                state_hash_by_entity={str(parent): "state-after"}
            ),
        )
        hole = creative_hole(image_local_ref(_CHECKSUM))
        # T112/R2.7: el unico origen legitimo de una resolucion es
        # `_execute_upload_creative`, que resuelve al manejador OPACO
        # (`platform_asset_id`, el `image_hash` de Meta) -- nunca una URL.
        resolved_image_hash = "a" * 32

        outcome = await fixture.run(
            _AD_STEP,
            parent_entity_ref=parent,
            parent_confirmed_state_hash="state-after",
            resolutions={hole: resolved_image_hash},
        )

        assert outcome.state == "done"
        sent = fixture.platform_write.calls[0].value
        link_data = sent["child_plan"]["native"]["creative_inline"]["object_story_spec"][
            "link_data"
        ]
        assert link_data["image_hash"] == resolved_image_hash

    async def test_activate_campaign_creates_nothing_and_flips_status(self) -> None:
        package = _package()
        parent = _campaign_ref(package)
        scope_ref = str(parent)
        fixture = await _fixture(
            package,
            guardrails_by_scope={scope_ref: _wide_open_guardrails(scope_ref)},
            platform_reader=FakePlatformReaderPort(
                state_hash_by_entity={str(parent): "state-after"}
            ),
        )

        outcome = await fixture.run(
            _ACTIVATE_STEP, parent_entity_ref=parent, parent_confirmed_state_hash="state-after"
        )

        assert outcome.state == "done"
        assert outcome.created_entity_ref is None
        assert fixture.platform_write.calls[0].before == "PAUSED"
        assert fixture.platform_write.calls[0].value == "ACTIVE"


class TestUploadCreativeForAssetGroupImages:
    """T044/T046 (BL-4/D-3): las imagenes del grupo de recursos de Maximo
    Rendimiento suben por el MISMO `_execute_upload_creative` que la
    creatividad de un anuncio -- `_find_asset_id_by_checksum` debe
    encontrar su `asset_id` (T044), y el manejador viaja con el MISMO
    `package_binding`/`package_approval` firmados que ya usa cualquier
    otro paso `UPLOAD_CREATIVE` (D-3, alineado con `_execute_upload_
    creative` de 5553635 sin tocar `broker/**`)."""

    async def test_find_asset_id_by_checksum_resuelve_imagenes_del_grupo_de_recursos(self) -> None:
        """Regresion (antes del arreglo): `_find_asset_id_by_checksum` solo
        recorria `ad_set.ads` -- un `checksum` que SOLO vive en
        `native.assets` (Maximo Rendimiento) devolvia `None`, y el paso
        fallaba con `creative_not_found` en vez de subir la imagen."""
        package = _package_with_asset_group()
        logo = package.ad_sets[0].native.assets.logo

        resolved = _find_asset_id_by_checksum(package, _PMAX_LOGO_CHECKSUM)

        assert resolved == str(logo.asset_id)

    async def test_sube_la_imagen_del_grupo_de_recursos_con_el_mismo_binding_y_aprobacion(
        self,
    ) -> None:
        package = _package_with_asset_group()
        logo_asset_id = str(package.ad_sets[0].native.assets.logo.asset_id)
        fixture = await _fixture(
            package,
            guardrails_by_scope={},
            creative_bytes=_FakeCreativeBytes({logo_asset_id: _PMAX_LOGO_MEDIA}),
        )
        upload_local_ref = image_local_ref(_PMAX_LOGO_CHECKSUM)
        upload_step_index = next(
            step.step_index
            for step in fixture.envelope.step_plan
            if step.local_ref == upload_local_ref
        )

        outcome = await fixture.run(upload_step_index)

        assert outcome.state == "done"
        assert outcome.created_entity_ref == "meta-image-hash-abc"
        upload_request = fixture.platform.calls[0]
        assert upload_request.package_binding["publication_id"] == fixture.envelope.publication_id
        assert upload_request.package_approval["envelope"]["package_id"] == str(
            package.package_id
        )


class TestPayloadReproducibilityIsCheckedBeforeAnyWrite:
    async def test_a_tampered_payload_template_hash_halts_without_proposing(self) -> None:
        package = _package()
        fixture = await _fixture(package, guardrails_by_scope={})
        tampered = replace(fixture.binding(_CAMPAIGN_STEP, None), payload_template_hash="0" * 64)

        outcome = await fixture.executor.execute_step(
            package=fixture.package,
            envelope=fixture.envelope,
            binding=tampered,
            resolutions={},
            human_authorization_id=fixture.human_authorization_id,
            human_approval_signature=fixture.human_approval_signature,
        )

        assert outcome.state == "failed"
        assert outcome.outcome_code == "package_payload_not_reproducible"
        assert fixture.execution_queue.saved == {}


class TestParentStateHashIsRequiredForChildSteps:
    """T123/AL-4: sin `parent_receipt_state_hash` (el `confirmed_state_hash`
    del recibo del padre) no hay precondicion que firmar para
    CREATE_AD_SET/CREATE_AD/ACTIVATE_CAMPAIGN -- falla cerrado ANTES de
    proponer nada, nunca con un valor inventado."""

    async def test_create_ad_set_without_a_parent_receipt_hash_halts_without_proposing(
        self,
    ) -> None:
        package = _package()
        parent = _campaign_ref(package)
        scope_ref = str(parent)
        fixture = await _fixture(
            package, guardrails_by_scope={scope_ref: _wide_open_guardrails(scope_ref)}
        )

        outcome = await fixture.run(_AD_SET_STEP, parent_entity_ref=parent)

        assert outcome.state == "failed"
        assert outcome.outcome_code == "package_parent_state_unconfirmed"
        assert fixture.execution_queue.saved == {}
        assert fixture.platform_write.calls == []

    async def test_activate_campaign_without_a_parent_receipt_hash_halts_without_proposing(
        self,
    ) -> None:
        package = _package()
        parent = _campaign_ref(package)
        scope_ref = str(parent)
        fixture = await _fixture(
            package, guardrails_by_scope={scope_ref: _wide_open_guardrails(scope_ref)}
        )

        outcome = await fixture.run(_ACTIVATE_STEP, parent_entity_ref=parent)

        assert outcome.state == "failed"
        assert outcome.outcome_code == "package_parent_state_unconfirmed"
        assert fixture.execution_queue.saved == {}

    async def test_create_campaign_never_needs_a_parent_receipt_hash(self) -> None:
        """CREATE_CAMPAIGN no tiene padre: sigue publicando sin el, como
        antes de este cierre."""
        package = _package()
        scope_ref = str(package.account_ref)
        fixture = await _fixture(
            package, guardrails_by_scope={scope_ref: _wide_open_guardrails(scope_ref)}
        )

        outcome = await fixture.run(_CAMPAIGN_STEP)

        assert outcome.state == "done"


class TestUploadCreativeErrorClassification:
    async def test_a_transient_network_error_is_unknown_not_a_halt(self) -> None:
        package = _package()
        asset_id = str(package.ad_sets[0].ads[0].creative.asset_id)
        fixture = await _fixture(
            package,
            guardrails_by_scope={},
            platform=_FakeUploadPlatform(error=ConnectionError("red inalcanzable")),
            creative_bytes=_FakeCreativeBytes({asset_id: _MEDIA}),
        )

        outcome = await fixture.run(_UPLOAD_STEP)

        assert outcome.state == "unknown"

    async def test_a_permanent_broker_denial_halts_the_saga(self) -> None:
        package = _package()
        asset_id = str(package.ad_sets[0].ads[0].creative.asset_id)
        fixture = await _fixture(
            package,
            guardrails_by_scope={},
            platform=_FakeUploadPlatform(
                error=BrokerRequestDeniedError("capability_not_implemented")
            ),
            creative_bytes=_FakeCreativeBytes({asset_id: _MEDIA}),
        )

        outcome = await fixture.run(_UPLOAD_STEP)

        assert outcome.state == "failed"
        assert outcome.outcome_code == "upload_denied:capability_not_implemented"


class TestIdempotencyKeyReuseOnRetry:
    async def test_retrying_a_materialised_step_never_proposes_or_writes_twice(self) -> None:
        package = _package()
        scope_ref = str(package.account_ref)
        fixture = await _fixture(
            package,
            guardrails_by_scope={scope_ref: _wide_open_guardrails(scope_ref)},
            created_external_id="123456",
        )

        first = await fixture.run(_CAMPAIGN_STEP)
        assert first.state == "done"
        assert len(fixture.execution_queue.saved) == 1
        assert len(fixture.platform_write.calls) == 1
        [attempt] = fixture.execution_queue.saved.values()
        expected_key = build_package_step_idempotency_key(fixture.envelope.publication_id, 1)
        assert attempt.idempotency_key == expected_key

        second = await fixture.run(_CAMPAIGN_STEP, existing_proposal_id=first.proposal_id)

        assert second.state == "done"
        assert second.created_entity_ref == first.created_entity_ref
        # Ni una segunda propuesta ni una segunda escritura al broker.
        assert len(fixture.execution_queue.saved) == 1
        assert len(fixture.platform_write.calls) == 1
