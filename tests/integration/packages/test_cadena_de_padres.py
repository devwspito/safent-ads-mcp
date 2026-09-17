"""T108 (R5, data-model.md Revision 2 §R2.4): un padre solo se admite desde
un recibo CONFIRMADO -- nunca desde lo que otro paso "dijera" en memoria ni
desde una fila manipulada. Dos capas INDEPENDIENTES, cada una probada por
separado:

1. `ads-api` (`campaign_package_steps_guard`, 0042): un `created_entity_ref`
   ya confirmado es INMUTABLE en la propia base de ads-api -- ni un UPDATE
   crudo que deje el `state` sin tocar puede reescribirlo. Primera linea de
   defensa, mas fuerte de lo que R5 exige por si sola.
2. El bróker (`broker.domain.package_admission.admit_package_step`, R5):
   NUNCA se fia de lo que `ads-api` afirme en el `binding` que le manda --
   resuelve el padre de su PROPIO libro (`resolve_created_resource`,
   `WriteLedgerStore.created_resource` en produccion). Aunque la capa 1 no
   existiera, o un `ads-api` comprometido mandase un `binding` con un padre
   inventado sin pasar por `RunPackagePublication`, el bróker lo deniega
   ANTES de tocar la plataforma -- defensa en profundidad, no la unica
   barrera."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.application.ports import (
    SignedAuthorization,
    WriteIntent,
    WriteOperation,
)
from safent_ads.accounts.application.refresh_registered_entity_state import (
    RefreshRegisteredEntityState,
)
from safent_ads.accounts.application.register_created_entity import RegisterCreatedEntity
from safent_ads.accounts.testing.in_memory_repositories import InMemoryAdEntityRepository
from safent_ads.broker.domain.package_admission import (
    admit_package_step,
    build_package_step_idempotency_key,
)
from safent_ads.broker.domain.write_authorization import WriteDenialCode
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
from safent_ads.packages.domain.campaign_package import CampaignPackage
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
from safent_ads.proposals.domain.diff_hash import canonical_json_bytes, compute_diff_hash
from safent_ads.proposals.testing.fakes import FakeSignerPort
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.crypto.ed25519 import ApprovalSigner, ApprovalVerifier
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode
from tests.conftest import BusinessFactory, OwnerFactory
from tests.integration.packages.conftest import SeededScope, seed_package_scope
from tests.unit.packages.application.test_approve_campaign_package import _wide_open_guardrails
from tests.unit.packages.domain.conftest import NOW, propose_meta_package

pytestmark = pytest.mark.integration

_CANCEL_GRACE_ELAPSED = NOW + timedelta(seconds=46)
_UPLOAD_STEP = 0
_CAMPAIGN_STEP = 1
_AD_SET_STEP = 2
_TRUE_CAMPAIGN_EXTERNAL_ID = "123456"
_FORGED_CAMPAIGN_ENTITY_REF = "meta:campaign:999999-forged"


def _done(created_entity_ref: str | None = None) -> StepExecutionOutcome:
    return StepExecutionOutcome(
        state="done", created_entity_ref=created_entity_ref, outcome_code=None
    )


class _SpyStepExecutor:
    """No ejecuta nada de verdad: solo archiva el `binding` que recibio en
    cada paso, para poder inspeccionar que `parent_entity_ref` ads-api
    resolvio ANTES de intentar ninguna escritura."""

    def __init__(self, outcomes: dict[int, StepExecutionOutcome]) -> None:
        self._outcomes = outcomes
        self.bindings: dict[int, PackageStepBinding] = {}

    async def execute_step(
        self, *, binding: PackageStepBinding, **_kwargs: object
    ) -> StepExecutionOutcome:
        self.bindings[binding.step_index] = binding
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

    def use_case(self, executor: _SpyStepExecutor) -> RunPackagePublication:
        entities = InMemoryAdEntityRepository()
        return RunPackagePublication(
            packages=self.packages,
            publications=self.publications,
            steps=self.steps,
            step_executor=executor,
            brakes=self.brakes,
            entity_registration=RegisterCreatedEntity(entities),
            entity_activation_refresh=RefreshRegisteredEntityState(entities),
            clock=self.clock,
        )


async def test_a_confirmed_receipt_cannot_be_tampered_with_and_the_parent_stays_the_real_one(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    owner_id = await owner_factory.create()
    business_id = await business_factory.create()
    scope = await seed_package_scope(db_session, owner_id=owner_id, business_id=business_id)
    scenario = Scenario(db_session, scope)
    publication_id = await scenario.approve()
    scenario.clock.advance_to(_CANCEL_GRACE_ELAPSED)

    executor = _SpyStepExecutor(
        {
            _UPLOAD_STEP: _done("meta-image-hash-abc"),
            _CAMPAIGN_STEP: _done(f"meta:campaign:{_TRUE_CAMPAIGN_EXTERNAL_ID}"),
            _AD_SET_STEP: _done("meta:ad_set:as-1"),
        }
    )
    run = scenario.use_case(executor)
    await run.execute(publication_id)  # paso 0
    await run.execute(publication_id)  # paso 1 (CREATE_CAMPAIGN), recibo real

    campaign_step = await scenario.steps.get(publication_id, _CAMPAIGN_STEP)
    assert campaign_step is not None
    assert campaign_step.created_entity_ref == f"meta:campaign:{_TRUE_CAMPAIGN_EXTERNAL_ID}"

    # Un atacante (o un bug de otra capa) con acceso a la base de ads-api
    # intenta manipular el recibo del paso 1 DIRECTAMENTE, sin pasar por
    # `SqlPackageStepRepository` -- mismo estado (`done`), otro
    # `created_entity_ref`. Primera capa de defensa, en la propia base de
    # ads-api: `campaign_package_steps_guard` (0042) hace INMUTABLE un
    # `created_entity_ref` ya confirmado -- ni siquiera un UPDATE crudo con
    # el estado sin tocar puede reescribirlo.
    with pytest.raises(DBAPIError, match="recibo confirmado es inmutable"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "UPDATE campaign_package_steps SET created_entity_ref = :forged "
                    "WHERE publication_id = :publication_id AND step_index = :step_index"
                ),
                {
                    "forged": _FORGED_CAMPAIGN_ENTITY_REF,
                    "publication_id": publication_id,
                    "step_index": _CAMPAIGN_STEP,
                },
            )

    # El recibo sigue siendo el real -- la manipulacion nunca se aplico --
    # y el paso 2 (CREATE_AD_SET) resuelve el padre VERDADERO, no el
    # falsificado que se intento colar.
    await run.execute(publication_id)  # paso 2 (CREATE_AD_SET)

    real_binding = executor.bindings[_AD_SET_STEP]
    assert real_binding.parent_entity_ref is not None
    assert real_binding.parent_entity_ref.external_id == _TRUE_CAMPAIGN_EXTERNAL_ID


def _keypair() -> tuple[ApprovalSigner, ApprovalVerifier]:
    seed_b64 = base64.b64encode(b"7" * 32).decode()
    signer = ApprovalSigner.from_seed_b64(seed_b64)
    return signer, ApprovalVerifier.from_public_key_b64(signer.public_key_b64())


def _hash_json(value: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def test_the_broker_denies_a_forged_parent_before_any_write(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    """La segunda capa, independiente de la primera: si -- pese al guardia
    de arriba, o por un `ads-api` comprometido que nunca paso por
    `RunPackagePublication` -- un `binding` con un padre inventado llegase
    al bróker, este lo deniega sin preguntarle nada a ads-api: resuelve el
    padre SOLO de su propio libro (`resolve_created_resource`), nunca del
    `binding` recibido."""
    del db_session, owner_factory, business_factory  # esta mitad es pura, sin E/S
    now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    later = now + timedelta(hours=1)
    business_id = "11111111-1111-1111-1111-111111111111"
    connection_id = "22222222-2222-2222-2222-222222222222"
    account_ref = EntityRef(
        PlatformCode.META,
        EntityLevel.ACCOUNT,
        "act_100",
        business_id,  # type: ignore[arg-type]
        connection_id,  # type: ignore[arg-type]
    )
    campaign_template = {"creation_plan": {"name": "Campaña de reserva"}}
    ad_set_template = {"child_plan": {"name": "Conjunto 1"}}
    step_plan = [
        {
            "step_index": 0,
            "step_kind": "CREATE_CAMPAIGN",
            "local_ref": "campaign",
            "parent_local_ref": None,
            "payload_template_hash": _hash_json(campaign_template),
            "expected_done_steps": None,
        },
        {
            "step_index": 1,
            "step_kind": "CREATE_AD_SET",
            "local_ref": "as#1",
            "parent_local_ref": "campaign",
            "payload_template_hash": _hash_json(ad_set_template),
            "expected_done_steps": None,
        },
    ]
    envelope = {
        "envelope_version": 2,
        "package_id": "pkg-1",
        "package_hash": "h" * 64,
        "business_id": business_id,
        "platform": "meta",
        "account_ref": str(account_ref),
        "publication_id": "pub-1",
        "approved_by": "owner-1",
        "approved_at": now.isoformat(),
        "approval_expires_at": later.isoformat(),
        "step_plan": step_plan,
    }
    signer, verifier = _keypair()
    signature = signer.sign(envelope)
    approval = {
        "envelope": envelope,
        "authorization_id": "human-auth-1",
        "issued_by": "owner-1",
        "expires_at": later.isoformat(),
        "signature": signature.hex(),
    }
    envelope_hash = _hash_json(envelope)
    forged_parent_ref = EntityRef.parse(_FORGED_CAMPAIGN_ENTITY_REF)
    ad_set_binding = {
        "package_id": "pkg-1",
        "package_hash": "h" * 64,
        "publication_id": "pub-1",
        "envelope_hash": envelope_hash,
        "step_index": 1,
        "step_kind": "CREATE_AD_SET",
        "local_ref": "as#1",
        "parent_local_ref": "campaign",
        "parent_step_index": 0,
        # Lo que ads-api afirma que es el padre -- exactamente el valor
        # falsificado que la prueba de arriba demostro que ads-api produce.
        "parent_entity_ref": str(forged_parent_ref),
        "account_ref": str(account_ref),
        "payload_template_hash": _hash_json(ad_set_template),
        "creative_sources": (),
        "expected_done_steps": None,
    }
    ad_set_intent = WriteIntent(
        entity_ref=forged_parent_ref,
        operation=WriteOperation.CREATE_AD_SET,
        parametro="new_ad_set:x",
        valor_actual=None,
        valor_propuesto=ad_set_template,
        diff_hash=compute_diff_hash(forged_parent_ref, "new_ad_set:x", None, ad_set_template),
        expected_state_hash="",
        business_id=business_id,
        package_binding=ad_set_binding,
    )

    authorization = SignedAuthorization(
        authorization_id="step-auth-1",
        proposal_id="proposal-1",
        kind="package_step",  # type: ignore[arg-type]
        diff_hash=ad_set_intent.diff_hash,
        guardrail_verdict_hash="v" * 64,
        issued_by="ads-worker",
        expires_at=later,
        signature="ab" * 32,
        package_approval=approval,
    )

    # El libro PROPIO del bróker: el paso 0 (CREATE_CAMPAIGN) SI se aplico
    # de verdad, y confirmo el recurso REAL -- distinto del que la fila
    # manipulada de ads-api afirma.
    true_campaign_key = build_package_step_idempotency_key("pub-1", 0)
    ledger = {true_campaign_key: _TRUE_CAMPAIGN_EXTERNAL_ID}

    denial = admit_package_step(
        intent=ad_set_intent,
        authorization=authorization,
        verifier=verifier,
        now=now,
        resolve_created_resource=ledger.get,
    )

    assert denial is WriteDenialCode.PACKAGE_PARENT_UNCONFIRMED
