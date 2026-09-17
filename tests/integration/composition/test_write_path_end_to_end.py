"""Prueba de humo de `quickstart.md §6` (F2, autonomia defensiva) contra
Postgres real y `Container.build_execution_use_cases` -- el mismo camino
que recorreria `RuleCycle`: senal viva -> regla `AUTO` -> `AuthorizeRuleAction`
-> se agenda y encola la ejecucion -> `ExecutionChokepoint.run_once()` ->
bróker real (socket Unix + `GoogleAdsAdapter` con el SDK sustituido) ->
entrada en `decision_log`.

Nada de esto esta falseado: `AuthorizeRuleAction`, `ExecutionChokepoint`,
`SqlProposalRepository`, `SqlAuthorizationRepository`, `SqlExecutionQueue`,
`SqlGuardrailSetRepository`, `SqlBrakeStatePort`, `SqlRuleConditionPort`,
`BrokerSocketClient`/`BrokerPlatformWriter` y `SqlDecisionRecorder` son
todos adaptadores reales, cableados exactamente como
`Container.build_execution_use_cases` los cablea en produccion. Desde que
`_build_write_pipeline`/`GoogleAdsAdapter.execute_write` aterrizaron
(composition/broker.py, broker/platforms/google_ads_adapter.py), un
`ADS_BROKER_SOCKET` real -- un `broker.presentation.socket_server.serve`
levantado dentro del propio test, con `GoogleAdsSearchClient` sustituido
por un doble en memoria (contracts/platform-port.md: "SDK mocked") -- deja
recorrer el camino completo hasta `EXECUTED`, no solo hasta la denegacion
de F1."""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from tests.conftest import rolled_back_session
from tests.contracts.execution.conftest import (
    FIRING_RULE_CODE,
    NOW,
    GuardrailLimits,
    calibrate_rule,
    digest,
    seed_freshness,
    seed_guardrails,
    seed_sell_signal,
)
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.unit.broker.ledger_scope_fakes import fake_scope
from tests.unit.composition.factories import build_api_settings

from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.audit.application.ports import DecisionLogFilter
from safent_ads.audit.domain.entry import DecisionKind
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.broker.application.app_credentials_service import AppCredentialsService
from safent_ads.broker.application.oauth_connect_flow import OAuthConnectFlow
from safent_ads.broker.domain.ledger_scope import LedgerScope
from safent_ads.broker.infrastructure.adapter_registry import PlatformAdapterRegistry
from safent_ads.broker.infrastructure.caps_config import parse_caps_config
from safent_ads.broker.infrastructure.credential_store import EncryptedCredentialStore
from safent_ads.broker.infrastructure.write_ledger_store import WriteLedgerStore
from safent_ads.broker.platforms.google_ads_adapter import GoogleAdsAdapter, GoogleAdsAdapterConfig
from safent_ads.broker.platforms.google_oauth_adapter import (
    GoogleOAuthAdapter,
    GoogleOAuthAdapterConfig,
)
from safent_ads.broker.platforms.meta_oauth_adapter import MetaOAuthAdapter, MetaOAuthAdapterConfig
from safent_ads.broker.platforms.write_pipeline import WriteAuthorizationPipeline
from safent_ads.broker.presentation.dispatcher import BrokerRuntime
from safent_ads.broker.presentation.socket_server import serve
from safent_ads.composition.container import Container
from safent_ads.execution.application.authorize_rule_action import AuthorizeRuleActionCommand
from safent_ads.execution.application.ports import WriteCommand
from safent_ads.execution.domain.execution_attempt import (
    ExecutionAttempt,
    ExecutionStatus,
    build_idempotency_key,
)
from safent_ads.execution.domain.guardrails import (
    GuardrailChange,
    GuardrailScope,
    ScopeKind,
    effective_diff,
)
from safent_ads.execution.domain.identifiers import ExecutionId
from safent_ads.execution.infrastructure.broker_platform import BrokerPlatformWriter
from safent_ads.execution.infrastructure.errors import PlatformWriteDeniedError
from safent_ads.execution.infrastructure.sql_spend_ledger import SqlSpendLedger
from safent_ads.proposals.application.submit_approval import SubmitApprovalCommand
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
from safent_ads.proposals.domain.proposal import (
    Proposal,
    ProposalState,
    ProposedDiff,
    new_proposal_id,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.crypto.ed25519 import ApprovalSigner, ApprovalVerifier
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

pytestmark = pytest.mark.integration

# Semilla de prueba para el par de firma Ed25519 de `ads-api`: el mismo
# material que `Container.build` deriva de `ApiSettings.approval_signing_key`
# (`composition/signing.py::build_approval_key_pair`), y del que este banco
# deriva de forma INDEPENDIENTE la clave publica del bróker -- igual que en
# produccion, donde la clave publica viaja como texto plano y nunca comparte
# proceso con la privada.
_APPROVAL_SEED_B64 = base64.b64encode(b"e" * 32).decode()
_APPROVAL_PUBLIC_KEY_B64 = ApprovalSigner.from_seed_b64(_APPROVAL_SEED_B64).public_key_b64()
_CREDENTIAL_MASTER_KEY_B64 = base64.b64encode(b"0" * 32).decode()


def _lower_budget_proposal(
    business_id: BusinessId, entity_ref: EntityRef, *, expected_state_hash: str | None = None
) -> Proposal:
    diff = ProposedDiff.build(
        entity_ref=entity_ref,
        parameter="daily_budget",
        before=Money.of("100"),
        after=Money.of("70"),
    )
    return Proposal.raise_proposal(
        proposal_id=new_proposal_id(),
        business_id=business_id,
        diff=diff,
        classification=Classification.ROUTINE,
        cause=Cause(text="ROAS por debajo del objetivo en 7D", rule_id=FIRING_RULE_CODE),
        cause_key=CauseKey(entity_ref=entity_ref, rule_id=FIRING_RULE_CODE, cause_type="roas_low"),
        evidence=(),
        estimated_impact=Money.of("310"),
        priority=Priority(urgency=Urgency.RECOMMENDED),
        now=NOW,
        expires_at=NOW.replace(hour=23),
        expected_state_hash=expected_state_hash,
    )


def _raise_budget_proposal(
    business_id: BusinessId, entity_ref: EntityRef, *, expected_state_hash: str | None = None
) -> Proposal:
    diff = ProposedDiff.build(
        entity_ref=entity_ref,
        parameter="daily_budget",
        before=Money.of("70"),
        after=Money.of("90"),
    )
    return Proposal.raise_proposal(
        proposal_id=new_proposal_id(),
        business_id=business_id,
        diff=diff,
        classification=Classification.ROUTINE,
        cause=Cause(text="Presupuesto infrautilizado en 7D", rule_id=FIRING_RULE_CODE),
        cause_key=CauseKey(
            entity_ref=entity_ref, rule_id=FIRING_RULE_CODE, cause_type="budget_underused"
        ),
        evidence=(),
        estimated_impact=Money.of("120"),
        priority=Priority(urgency=Urgency.RECOMMENDED),
        now=NOW,
        expires_at=NOW.replace(hour=23),
        expected_state_hash=expected_state_hash,
    )


def _drifted_budget_proposal(
    business_id: BusinessId,
    entity_ref: EntityRef,
    *,
    before: Money,
    after: Money,
    expected_state_hash: str | None = None,
) -> Proposal:
    """Como `_lower_budget_proposal`/`_raise_budget_proposal`, pero con
    `before`/`after` explicitos: modela una entidad cuyo presupuesto en
    vivo ya driftó fuera de suelo/techo antes de que el guardarraíl la
    vea -- el escenario del BUG corregido, donde el recorte puede invertir
    la direccion real del cambio frente al diff CRUDO."""
    diff = ProposedDiff.build(
        entity_ref=entity_ref, parameter="daily_budget", before=before, after=after
    )
    return Proposal.raise_proposal(
        proposal_id=new_proposal_id(),
        business_id=business_id,
        diff=diff,
        classification=Classification.ROUTINE,
        cause=Cause(text="Presupuesto infrautilizado en 7D", rule_id=FIRING_RULE_CODE),
        cause_key=CauseKey(
            entity_ref=entity_ref, rule_id=FIRING_RULE_CODE, cause_type="budget_underused"
        ),
        evidence=(),
        estimated_impact=Money.of("120"),
        priority=Priority(urgency=Urgency.RECOMMENDED),
        now=NOW,
        expires_at=NOW.replace(hour=23),
        expected_state_hash=expected_state_hash,
    )


def _campaign_row(*, customer_id: str, campaign_id: str, amount_micros: int) -> dict[str, Any]:
    return {
        "campaign.resource_name": f"customers/{customer_id}/campaigns/{campaign_id}",
        "campaign.id": int(campaign_id),
        "campaign.name": "Campana de contrato",
        "campaign.status": "ENABLED",
        "campaign_budget.resource_name": f"customers/{customer_id}/campaignBudgets/{campaign_id}",
        "campaign_budget.amount_micros": amount_micros,
        "campaign_budget.type": "STANDARD",
        "campaign_budget.explicitly_shared": False,
        "customer.currency_code": "EUR",
    }


def _campaign_state_hash(row: Mapping[str, Any]) -> str:
    canonical_state = {
        key: value for key, value in row.items() if not key.endswith(".resource_name")
    }
    return PlatformStateHash.compute(canonical_state).value


def _caps_yaml(customer_id: str) -> str:
    """Topes generosos a proposito: estos bancos prueban los controles de
    firma/deriva/kind, no los topes duros (ya cubiertos en
    `tests/unit/broker/platforms/test_write_pipeline.py`)."""
    return dedent(
        f"""
        defaults:
          max_step_pct: 100
          max_changes_per_day: 5
          autonomy_enabled: true
        accounts:
          "{customer_id}":
            daily_cap_minor: 100000000
            monthly_cap_minor: 1000000000
            floor_minor: 100
            ceiling_minor: 100000000
        """
    )


class _FakeGoogleSearchClient:
    """`GoogleAdsSearchClient` sustituido por un doble en memoria
    (contracts/platform-port.md: "SDK mocked in tests") -- ninguna llamada
    de este banco toca la red real."""

    def __init__(self, campaign_row: Mapping[str, Any]) -> None:
        self._campaign_row = campaign_row
        self.budget_mutations: list[tuple[str, str, int]] = []

    def search_stream(
        self,
        customer_id: str,  # noqa: ARG002 - doble de pruebas, firma del puerto
        query: str,  # noqa: ARG002 - unica entidad servida por este doble
    ) -> Iterator[Mapping[str, Any]]:
        yield self._campaign_row

    def mutate_campaign_budget(
        self, customer_id: str, budget_resource_name: str, amount_micros: int
    ) -> str:
        self.budget_mutations.append((customer_id, budget_resource_name, amount_micros))
        return budget_resource_name

    def mutate_status(
        self,
        customer_id: str,  # noqa: ARG002 - forma exacta del puerto, no ejercitado por este banco
        resource_name: str,  # noqa: ARG002
        level: EntityLevel,  # noqa: ARG002
        status: str,  # noqa: ARG002
    ) -> str:
        raise NotImplementedError

    def mutate_negative_keyword(
        self,
        customer_id: str,  # noqa: ARG002 - forma exacta del puerto, no ejercitado por este banco
        ad_group_resource_name: str,  # noqa: ARG002
        keyword_text: str,  # noqa: ARG002
    ) -> str:
        raise NotImplementedError


class _UnreachableHttpClient:
    """Estos bancos nunca ejercitan un `op` de OAuth: si algo llegara a
    llamar al cliente HTTP, es un fallo del banco, no un doble legitimo."""

    async def post_form(self, url: str, *, data: dict[str, str]) -> dict[str, object]:  # noqa: ARG002
        raise AssertionError

    async def get_json(
        self,
        url: str,  # noqa: ARG002 - doble de pruebas, firma del puerto
        *,
        headers: object = None,  # noqa: ARG002 - doble de pruebas, firma del puerto
        params: object = None,  # noqa: ARG002
    ) -> dict[str, object]:
        raise AssertionError

    async def post_json(
        self,
        url: str,  # noqa: ARG002 - doble de pruebas, firma del puerto
        *,
        json_body: object,  # noqa: ARG002 - doble de pruebas, firma del puerto
        headers: object = None,  # noqa: ARG002
    ) -> dict[str, object]:
        raise AssertionError


def _broker_runtime(registry: PlatformAdapterRegistry, credential_store_dir: Path) -> BrokerRuntime:
    clock = FixedClock(NOW)
    store = EncryptedCredentialStore(credential_store_dir, _CREDENTIAL_MASTER_KEY_B64)
    google_oauth = GoogleOAuthAdapter(
        GoogleOAuthAdapterConfig(client_id="c", client_secret="s"),
        _UnreachableHttpClient(),  # type: ignore[arg-type]
        clock,
    )
    meta_oauth = MetaOAuthAdapter(
        MetaOAuthAdapterConfig(app_id="a", app_secret="s"),
        _UnreachableHttpClient(),
        clock,  # type: ignore[arg-type]
    )
    return BrokerRuntime(
        adapters=registry,
        oauth_flow=OAuthConnectFlow(store, google_oauth, meta_oauth, clock),
        app_credentials=AppCredentialsService(store, clock),
    )


@asynccontextmanager
async def _running_broker(
    tmp_path: Path, *, search_client: _FakeGoogleSearchClient, customer_id: str
) -> AsyncIterator[Path]:
    """Levanta `ads-broker` de verdad sobre un socket Unix del directorio
    temporal: `ApprovalVerifier` (clave publica derivada de forma
    independiente de la semilla de `ads-api`) + `CapsConfig` +
    `WriteLedgerStore`, exactamente como los cablea
    `composition.broker._build_write_pipeline`."""
    socket_path = tmp_path / "broker.sock"
    verifier = ApprovalVerifier.from_public_key_b64(_APPROVAL_PUBLIC_KEY_B64)
    caps = parse_caps_config(_caps_yaml(customer_id))
    ledger = WriteLedgerStore(tmp_path / "write_ledger.sqlite3")
    pipeline = WriteAuthorizationPipeline(verifier, caps, ledger, scope_resolver=fake_scope)
    config = GoogleAdsAdapterConfig(
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="refresh-token",
        login_customer_id="",
    )
    adapter = GoogleAdsAdapter(config, search_client, FixedClock(NOW), write_pipeline=pipeline)
    registry = PlatformAdapterRegistry({PlatformCode.GOOGLE: adapter})
    runtime = _broker_runtime(registry, tmp_path / "credentials")
    server = await serve(socket_path, runtime, frozenset({os.getuid()}))
    try:
        yield socket_path
    finally:
        server.close()
        await server.wait_closed()


async def test_signal_to_unreachable_broker_fails_end_to_end(
    isolated_database_url: str,
) -> None:
    """Sin bróker escuchando, `BrokerSocketClient` falla con un error de
    infraestructura real (no un `WriteOutcome` inventado); el chokepoint lo
    trata como cualquier otro fallo del adaptador -- default-deny (C-1),
    nunca `EXECUTED`."""
    settings = build_api_settings(
        database_url=isolated_database_url,
        broker_socket_path="/tmp/safent-ads-wiring-b-test/does-not-exist.sock",
    )
    # `rolled_back_session`, no `container.session_factory()`: `SqlUnitOfWork`
    # (dentro del chokepoint) confirma su propia transaccion a mitad de
    # camino a proposito (C-15) -- sin la sesion-sobre-savepoint de este
    # ayudante, ese commit se filtraria a `isolated_database_url` de verdad,
    # incluida la calibracion GLOBAL de la regla M05 que otros tests de este
    # mismo banco esperan encontrar apagada.
    container = Container.build(settings)
    container.clock = FixedClock(NOW)
    try:
        async with rolled_back_session(isolated_database_url) as session:
            entity_ref = campaign_ref(f"c-{NOW.timestamp():.0f}")
            business_id = await seed_entity(session, entity_ref)
            await seed_freshness(session, entity_ref, lag_minutes=5)
            await seed_sell_signal(session, entity_ref)
            await calibrate_rule(session, enabled=True)
            await seed_guardrails(
                session,
                scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref)),
                limits=GuardrailLimits(),
                level="business",
            )

            proposal = _lower_budget_proposal(BusinessId(business_id), entity_ref)
            use_cases = container.build_execution_use_cases(session)
            await use_cases.proposals.save(proposal)

            authorization = await use_cases.authorize_rule_action.execute(
                AuthorizeRuleActionCommand(proposal.proposal_id, FIRING_RULE_CODE)
            )
            assert authorization is not None

            reloaded = await use_cases.proposals.get(proposal.proposal_id)
            assert reloaded is not None
            assert reloaded.state is ProposalState.APPROVED

            reloaded.schedule_execution(0, NOW)
            await use_cases.proposals.save(reloaded)
            await use_cases.execution_queue.save(
                ExecutionAttempt(
                    execution_id=ExecutionId.new(),
                    business_id=reloaded.business_id,
                    proposal_id=reloaded.proposal_id,
                    authorization_id=authorization.authorization_id,
                    idempotency_key=(f"exec-{reloaded.proposal_id}-{reloaded.diff.diff_hash[:12]}"),
                    # `executions.platform_state_hash_before` es NOT NULL; la
                    # propuesta se dejo sin `expected_state_hash` a proposito
                    # (ver docstring del modulo) para que
                    # `PlatformStateRevalidator.has_drifted` no necesite
                    # hablar con el broker -- son dos campos independientes,
                    # el intento SI necesita el suyo para persistir.
                    platform_state_hash_before=digest(f"state-{reloaded.proposal_id}"),
                )
            )

            outcome = await use_cases.chokepoint.run_once()

            assert outcome is ExecutionStatus.UNKNOWN

            executed = await use_cases.proposals.get(proposal.proposal_id)
            assert executed is not None
            assert executed.state is ProposalState.EXECUTING

            decision_log = await SqlDecisionLogRepository(session).search(
                DecisionLogFilter(business_id=BusinessId(business_id), kind=DecisionKind.EXECUTION)
            )
            assert len(decision_log.entries) >= 1
            entry = decision_log.entries[0]
            assert entry.payload["outcome"] == "unknown"
            assert entry.payload["error_code"] == "remote_outcome_unknown"
            # C-31/threat-model: nunca PII en el payload de decision_log.
            assert "email" not in entry.payload
            assert "phone" not in entry.payload
    finally:
        await container.aclose()


async def test_lower_budget_with_owner_approval_executes_end_to_end(
    isolated_database_url: str, tmp_path: Path
) -> None:
    """El camino completo hasta `EXECUTED`: senal viva -> regla `AUTO`
    (M05, ROAS bajo) -> `rule_authorization` firmada -> bróker real con
    `GoogleAdsAdapter`/SDK sustituido -> mutacion aplicada, ledger propio
    del bróker anotado, `decision_log` con el desenlace, y una repeticion
    con la MISMA `idempotency_key` que no vuelve a mutar."""
    customer_id = "9100000001"
    row = _campaign_row(customer_id=customer_id, campaign_id="1", amount_micros=100_000_000)
    expected_state_hash = _campaign_state_hash(row)
    search_client = _FakeGoogleSearchClient(row)

    async with _running_broker(
        tmp_path, search_client=search_client, customer_id=customer_id
    ) as socket_path:
        settings = build_api_settings(
            database_url=isolated_database_url,
            broker_socket_path=str(socket_path),
            approval_signing_key=_APPROVAL_SEED_B64,
        )
        container = Container.build(settings)
        container.clock = FixedClock(NOW)
        try:
            async with rolled_back_session(isolated_database_url) as session:
                entity_ref = campaign_ref(
                    f"customers/{customer_id}/campaigns/1", platform_value="google"
                )
                business_id = await seed_entity(session, entity_ref)
                await seed_freshness(session, entity_ref, lag_minutes=5)
                await seed_sell_signal(session, entity_ref)
                await calibrate_rule(session, enabled=True)
                await seed_guardrails(
                    session,
                    scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref)),
                    limits=GuardrailLimits(),
                    level="business",
                )

                proposal = _lower_budget_proposal(
                    BusinessId(business_id), entity_ref, expected_state_hash=expected_state_hash
                )
                use_cases = container.build_execution_use_cases(session)
                await use_cases.proposals.save(proposal)

                approval = await use_cases.submit_approval.execute(
                    SubmitApprovalCommand(
                        proposal_id=proposal.proposal_id,
                        diff_hash=proposal.diff.diff_hash,
                        approved_by="owner-qa",
                        channel=AuthorizationChannel.PANEL,
                    )
                )
                authorization = await use_cases.authorizations.get(
                    AuthorizationId.parse(approval.authorization_id)
                )
                assert authorization is not None
                reloaded = await use_cases.proposals.get(proposal.proposal_id)
                assert reloaded is not None
                idempotency_key = build_idempotency_key(
                    proposal.proposal_id, proposal.diff.diff_hash
                )
                container.clock.advance_to(approval.execution_scheduled_at)  # type: ignore[attr-defined]
                outcome = await use_cases.chokepoint.run_once(proposal_id=proposal.proposal_id)

                assert outcome is ExecutionStatus.EXECUTED

                executed = await use_cases.proposals.get(proposal.proposal_id)
                assert executed is not None
                assert executed.state is ProposalState.EXECUTED

                decision_log = await SqlDecisionLogRepository(session).search(
                    DecisionLogFilter(
                        business_id=BusinessId(business_id), kind=DecisionKind.EXECUTION
                    )
                )
                assert len(decision_log.entries) >= 1
                assert decision_log.entries[0].payload["outcome"] == "executed"

                # El "doble libro" propio del bróker (threat-model.md C-17):
                # un unico cambio anotado, con el delta real (100 -> 70 EUR).
                ledger = WriteLedgerStore(tmp_path / "write_ledger.sqlite3")
                snapshot = ledger.snapshot_today(
                    LedgerScope(business_id, PlatformCode.GOOGLE, customer_id), NOW.date()
                )
                assert snapshot.changes_count == 1
                assert snapshot.applied_delta_minor_units == -3_000
                assert len(search_client.budget_mutations) == 1

                # Repetir con la MISMA idempotency_key no vuelve a mutar.
                platform_write = BrokerPlatformWriter(
                    container.ads_platform_port, use_cases.proposals
                )
                command = WriteCommand(
                    entity_ref=reloaded.diff.entity_ref,
                    parameter=reloaded.diff.parameter,
                    before=reloaded.diff.before,
                    value=reloaded.diff.after,
                )
                replay_result = await platform_write.execute_write(
                    command, authorization, idempotency_key
                )
                assert replay_result.confirmed_state_hash is not None
                assert len(search_client.budget_mutations) == 1
        finally:
            await container.aclose()


async def test_raise_budget_with_rule_authorization_is_denied_by_the_broker(
    isolated_database_url: str, tmp_path: Path
) -> None:
    """FR-11/threat-model.md T-60: ninguna `rule_authorization` autoriza
    subir gasto. `execution.domain.guardrails.GuardrailEvaluator` ya lo
    bloquea antes de firmar (`auto_action_would_increase_spend`) -- este
    banco prueba la defensa en profundidad del PROPIO bróker
    (`RULE_AUTHORIZATION_CANNOT_INCREASE_SPEND`), firmando de verdad con el
    material de `ads-api` una autorizacion que, si llegase, el bróker debe
    seguir denegando por su cuenta."""
    customer_id = "9100000002"
    row = _campaign_row(customer_id=customer_id, campaign_id="1", amount_micros=70_000_000)
    expected_state_hash = _campaign_state_hash(row)
    search_client = _FakeGoogleSearchClient(row)

    async with _running_broker(
        tmp_path, search_client=search_client, customer_id=customer_id
    ) as socket_path:
        settings = build_api_settings(
            database_url=isolated_database_url,
            broker_socket_path=str(socket_path),
            approval_signing_key=_APPROVAL_SEED_B64,
        )
        container = Container.build(settings)
        container.clock = FixedClock(NOW)
        try:
            async with rolled_back_session(isolated_database_url) as session:
                entity_ref = campaign_ref(
                    f"customers/{customer_id}/campaigns/1", platform_value="google"
                )
                business_id = await seed_entity(session, entity_ref)

                proposal = _raise_budget_proposal(
                    BusinessId(business_id), entity_ref, expected_state_hash=expected_state_hash
                )
                use_cases = container.build_execution_use_cases(session)
                await use_cases.proposals.save(proposal)

                authorization = sign_authorization(
                    authorization_id=AuthorizationId.new(),
                    proposal_id=proposal.proposal_id,
                    kind=AuthorizationKind.RULE_AUTHORIZATION,
                    proposal_classification=proposal.classification,
                    diff_hash=proposal.diff.diff_hash,
                    guardrail_verdict_hash=digest("veredicto-omitido-a-proposito"),
                    issued_by=FIRING_RULE_CODE,
                    channel=AuthorizationChannel.RULE_ENGINE,
                    decided_at=NOW,
                    expires_at=NOW + timedelta(minutes=15),
                    signer=container.approval_key_pair.signer,
                )
                idempotency_key = f"exec-{proposal.proposal_id}-{proposal.diff.diff_hash[:12]}"

                platform_write = BrokerPlatformWriter(
                    container.ads_platform_port, use_cases.proposals
                )
                command = WriteCommand(
                    entity_ref=proposal.diff.entity_ref,
                    parameter=proposal.diff.parameter,
                    before=proposal.diff.before,
                    value=proposal.diff.after,
                )

                with pytest.raises(PlatformWriteDeniedError) as denied:
                    await platform_write.execute_write(command, authorization, idempotency_key)

                assert denied.value.error_code == "rule_authorization_cannot_increase_spend"
                assert search_client.budget_mutations == []
        finally:
            await container.aclose()


async def test_raise_budget_with_human_approval_executes_end_to_end(
    isolated_database_url: str, tmp_path: Path
) -> None:
    """Lo que una `rule_authorization` nunca puede hacer, un
    `human_approval` si: subir presupuesto llega a `EXECUTED` cuando un
    propietario lo autoriza explicitamente y el guardarraíl en vivo lo
    permite."""
    customer_id = "9100000003"
    row = _campaign_row(customer_id=customer_id, campaign_id="1", amount_micros=70_000_000)
    expected_state_hash = _campaign_state_hash(row)
    search_client = _FakeGoogleSearchClient(row)

    async with _running_broker(
        tmp_path, search_client=search_client, customer_id=customer_id
    ) as socket_path:
        settings = build_api_settings(
            database_url=isolated_database_url,
            broker_socket_path=str(socket_path),
            approval_signing_key=_APPROVAL_SEED_B64,
        )
        container = Container.build(settings)
        container.clock = FixedClock(NOW)
        try:
            async with rolled_back_session(isolated_database_url) as session:
                entity_ref = campaign_ref(
                    f"customers/{customer_id}/campaigns/1", platform_value="google"
                )
                business_id = await seed_entity(session, entity_ref)
                scope = GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref))
                await seed_guardrails(
                    session, scope=scope, limits=GuardrailLimits(), level="business"
                )

                proposal = _raise_budget_proposal(
                    BusinessId(business_id), entity_ref, expected_state_hash=expected_state_hash
                )
                use_cases = container.build_execution_use_cases(session)
                await use_cases.proposals.save(proposal)

                guardrails = await use_cases.guardrail_sets.get_effective(scope)
                spend_ledger = SqlSpendLedger(session, container.clock, use_cases.execution_queue)
                ledger_snapshot = await spend_ledger.snapshot(scope, entity_ref)
                change = GuardrailChange(
                    scope=scope,
                    entity_ref=entity_ref,
                    authorization_kind=AuthorizationKind.HUMAN_APPROVAL,
                    before=Money.of("70"),
                    after=Money.of("90"),
                )
                verdict = use_cases.guardrail_evaluator.evaluate(
                    change, guardrails, ledger_snapshot
                )
                assert verdict.allowed

                authorization = sign_authorization(
                    authorization_id=AuthorizationId.new(),
                    proposal_id=proposal.proposal_id,
                    kind=AuthorizationKind.HUMAN_APPROVAL,
                    proposal_classification=proposal.classification,
                    diff_hash=proposal.diff.diff_hash,
                    guardrail_verdict_hash=verdict.verdict_hash,
                    issued_by="owner-de-contrato",
                    channel=AuthorizationChannel.PANEL,
                    decided_at=NOW,
                    expires_at=NOW + timedelta(hours=1),
                    signer=container.approval_key_pair.signer,
                )
                await use_cases.authorizations.save(authorization)
                proposal.approve(proposal.diff.diff_hash, NOW)
                await use_cases.proposals.save(proposal)
                proposal.schedule_execution(0, NOW)
                await use_cases.proposals.save(proposal)

                idempotency_key = f"exec-{proposal.proposal_id}-{proposal.diff.diff_hash[:12]}"
                await use_cases.execution_queue.save(
                    ExecutionAttempt(
                        execution_id=ExecutionId.new(),
                        business_id=proposal.business_id,
                        proposal_id=proposal.proposal_id,
                        authorization_id=authorization.authorization_id,
                        idempotency_key=idempotency_key,
                        previous_value=Money.of("70"),
                        platform_state_hash_before=digest(f"state-{proposal.proposal_id}"),
                    )
                )

                outcome = await use_cases.chokepoint.run_once()

                assert outcome is ExecutionStatus.EXECUTED

                executed = await use_cases.proposals.get(proposal.proposal_id)
                assert executed is not None
                assert executed.state is ProposalState.EXECUTED
                assert len(search_client.budget_mutations) == 1
        finally:
            await container.aclose()


async def test_a_guardrail_clamp_that_flips_the_direction_executes_with_the_effective_operation(  # noqa: PLR0915
    isolated_database_url: str, tmp_path: Path
) -> None:
    """El escenario exacto del BUG corregido (threat-model.md C-15/C-17):
    `before`=350 ya esta por encima del techo (300, `GuardrailLimits` por
    defecto) por deriva externa, y la propuesta pide subirlo aun mas, a
    400. El guardarraíl recorta el EFECTIVO al techo -- 300, MENOR que
    `before` -- asi que la escritura real es una BAJADA, no la subida que
    el diff CRUDO sugiere. Antes de este fix, `BrokerPlatformWriter`
    clasificaba `RAISE_BUDGET` desde el diff crudo (350 -> 400); con el
    fix, el bróker recibe `LOWER_BUDGET` de 350 a 300 y ejecuta con el
    valor RECORTADO de principio a fin: SDK, `decision_log`, y el doble
    libro del propio bróker."""
    customer_id = "9100000004"
    row = _campaign_row(customer_id=customer_id, campaign_id="1", amount_micros=350_000_000)
    expected_state_hash = _campaign_state_hash(row)
    search_client = _FakeGoogleSearchClient(row)

    async with _running_broker(
        tmp_path, search_client=search_client, customer_id=customer_id
    ) as socket_path:
        settings = build_api_settings(
            database_url=isolated_database_url,
            broker_socket_path=str(socket_path),
            approval_signing_key=_APPROVAL_SEED_B64,
        )
        container = Container.build(settings)
        container.clock = FixedClock(NOW)
        try:
            async with rolled_back_session(isolated_database_url) as session:
                entity_ref = campaign_ref(
                    f"customers/{customer_id}/campaigns/1", platform_value="google"
                )
                business_id = await seed_entity(session, entity_ref)
                scope = GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref))
                await seed_guardrails(
                    session, scope=scope, limits=GuardrailLimits(), level="business"
                )

                proposal = _drifted_budget_proposal(
                    BusinessId(business_id),
                    entity_ref,
                    before=Money.of("350"),
                    after=Money.of("400"),
                    expected_state_hash=expected_state_hash,
                )
                use_cases = container.build_execution_use_cases(session)
                await use_cases.proposals.save(proposal)

                guardrails = await use_cases.guardrail_sets.get_effective(scope)
                spend_ledger = SqlSpendLedger(session, container.clock, use_cases.execution_queue)
                ledger_snapshot = await spend_ledger.snapshot(scope, entity_ref)
                change = GuardrailChange(
                    scope=scope,
                    entity_ref=entity_ref,
                    authorization_kind=AuthorizationKind.HUMAN_APPROVAL,
                    before=Money.of("350"),
                    after=Money.of("400"),
                )
                verdict = use_cases.guardrail_evaluator.evaluate(
                    change, guardrails, ledger_snapshot
                )
                assert verdict.allowed
                assert verdict.clamped_after == Money.of("300")

                # Firmado con el hash del diff EFECTIVO (300), no del diff
                # CRUDO de la propuesta (400) -- lo mismo que hacen de
                # verdad `AuthorizeRuleAction`/`SubmitApproval`.
                effective_diff_hash = effective_diff(proposal.diff, verdict).diff_hash
                authorization = sign_authorization(
                    authorization_id=AuthorizationId.new(),
                    proposal_id=proposal.proposal_id,
                    kind=AuthorizationKind.HUMAN_APPROVAL,
                    proposal_classification=proposal.classification,
                    diff_hash=effective_diff_hash,
                    guardrail_verdict_hash=verdict.verdict_hash,
                    issued_by="owner-de-contrato",
                    channel=AuthorizationChannel.PANEL,
                    decided_at=NOW,
                    expires_at=NOW + timedelta(hours=1),
                    signer=container.approval_key_pair.signer,
                )
                await use_cases.authorizations.save(authorization)
                proposal.approve(proposal.diff.diff_hash, NOW)
                await use_cases.proposals.save(proposal)
                proposal.schedule_execution(0, NOW)
                await use_cases.proposals.save(proposal)

                idempotency_key = f"exec-{proposal.proposal_id}-{proposal.diff.diff_hash[:12]}"
                await use_cases.execution_queue.save(
                    ExecutionAttempt(
                        execution_id=ExecutionId.new(),
                        business_id=proposal.business_id,
                        proposal_id=proposal.proposal_id,
                        authorization_id=authorization.authorization_id,
                        idempotency_key=idempotency_key,
                        previous_value=Money.of("350"),
                        platform_state_hash_before=digest(f"state-{proposal.proposal_id}"),
                    )
                )

                outcome = await use_cases.chokepoint.run_once()

                assert outcome is ExecutionStatus.EXECUTED

                executed = await use_cases.proposals.get(proposal.proposal_id)
                assert executed is not None
                assert executed.state is ProposalState.EXECUTED

                # El SDK recibe el valor RECORTADO (300), nunca los 400
                # crudos de la propuesta.
                assert len(search_client.budget_mutations) == 1
                assert search_client.budget_mutations[0][2] == 300_000_000

                # Confirmacion leida tras aplicar (FR-21): el intento
                # ejecutado guarda el hash remoto posterior y el valor
                # RECORTADO que de verdad se aplico, no los 400 crudos.
                confirmed = await use_cases.execution_queue.get_for_proposal(proposal.proposal_id)
                assert confirmed is not None
                assert confirmed.platform_state_hash_after is not None
                assert confirmed.applied_value == Money.of("300").to_canonical()

                # Doble libro del propio bróker (threat-model.md C-17): el
                # delta anotado es el EFECTIVO (350 -> 300 EUR), una bajada
                # -- nunca los 350 -> 400 del diff crudo.
                ledger = WriteLedgerStore(tmp_path / "write_ledger.sqlite3")
                snapshot = ledger.snapshot_today(
                    LedgerScope(business_id, PlatformCode.GOOGLE, customer_id), NOW.date()
                )
                assert snapshot.changes_count == 1
                assert snapshot.applied_delta_minor_units == -5_000
        finally:
            await container.aclose()


async def test_a_clamp_below_the_floor_that_flips_to_a_spend_increase_is_rejected_twice(
    isolated_database_url: str, tmp_path: Path
) -> None:
    """Caso espejo del anterior: `before`=8 ya esta por debajo del suelo (10)
    por deriva externa y una regla `AUTO` pide bajarlo a 5. El recorte al
    suelo lo dejaria en 10: una SUBIDA real (8 -> 10). Dos redes, y las dos
    se prueban aqui: (1) `GuardrailEvaluator` rechaza la accion porque el
    valor EFECTIVO sube gasto (FR-11 sobre el valor recortado, no el
    propuesto); (2) si esa capa se saltase -- se simula firmando como
    `RULE_AUTHORIZATION` un veredicto obtenido como aprobacion humana --,
    el propio broker clasifica `RAISE_BUDGET` desde el diff efectivo y
    deniega con `RULE_AUTHORIZATION_CANNOT_INCREASE_SPEND`. Antes del fix
    de clasificacion, el broker veia `LOWER_BUDGET` (8 -> 5 crudo) y la
    escritura llegaba a `EXECUTED` subiendo el presupuesto sin autorizacion."""
    customer_id = "9100000005"
    row = _campaign_row(customer_id=customer_id, campaign_id="1", amount_micros=8_000_000)
    expected_state_hash = _campaign_state_hash(row)
    search_client = _FakeGoogleSearchClient(row)

    async with _running_broker(
        tmp_path, search_client=search_client, customer_id=customer_id
    ) as socket_path:
        settings = build_api_settings(
            database_url=isolated_database_url,
            broker_socket_path=str(socket_path),
            approval_signing_key=_APPROVAL_SEED_B64,
        )
        container = Container.build(settings)
        container.clock = FixedClock(NOW)
        try:
            async with rolled_back_session(isolated_database_url) as session:
                entity_ref = campaign_ref(
                    f"customers/{customer_id}/campaigns/1", platform_value="google"
                )
                business_id = await seed_entity(session, entity_ref)
                scope = GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref))
                await seed_guardrails(
                    session, scope=scope, limits=GuardrailLimits(), level="business"
                )

                proposal = _drifted_budget_proposal(
                    BusinessId(business_id),
                    entity_ref,
                    before=Money.of("8"),
                    after=Money.of("5"),
                    expected_state_hash=expected_state_hash,
                )
                use_cases = container.build_execution_use_cases(session)
                await use_cases.proposals.save(proposal)

                guardrails = await use_cases.guardrail_sets.get_effective(scope)
                spend_ledger = SqlSpendLedger(session, container.clock, use_cases.execution_queue)
                ledger_snapshot = await spend_ledger.snapshot(scope, entity_ref)
                change = GuardrailChange(
                    scope=scope,
                    entity_ref=entity_ref,
                    authorization_kind=AuthorizationKind.RULE_AUTHORIZATION,
                    before=Money.of("8"),
                    after=Money.of("5"),
                )
                verdict = use_cases.guardrail_evaluator.evaluate(
                    change, guardrails, ledger_snapshot
                )
                # Red 1: el guardarrail de ejecucion rechaza la subida efectiva.
                assert verdict.allowed is False
                assert verdict.clamped_after == Money.of("8")
                assert "auto_action_would_increase_spend_after_clamp" in verdict.reasons

                # Red 2: se simula un salto de la capa anterior firmando como
                # regla un veredicto que solo una aprobacion humana obtendria.
                bypass_verdict = use_cases.guardrail_evaluator.evaluate(
                    replace(change, authorization_kind=AuthorizationKind.HUMAN_APPROVAL),
                    guardrails,
                    ledger_snapshot,
                )
                assert bypass_verdict.allowed
                assert bypass_verdict.clamped_after == Money.of("10")

                effective_diff_hash = effective_diff(proposal.diff, bypass_verdict).diff_hash
                authorization = sign_authorization(
                    authorization_id=AuthorizationId.new(),
                    proposal_id=proposal.proposal_id,
                    kind=AuthorizationKind.RULE_AUTHORIZATION,
                    proposal_classification=proposal.classification,
                    diff_hash=effective_diff_hash,
                    guardrail_verdict_hash=bypass_verdict.verdict_hash,
                    issued_by=FIRING_RULE_CODE,
                    channel=AuthorizationChannel.RULE_ENGINE,
                    decided_at=NOW,
                    expires_at=NOW + timedelta(minutes=15),
                    signer=container.approval_key_pair.signer,
                )
                idempotency_key = f"exec-{proposal.proposal_id}-{proposal.diff.diff_hash[:12]}"

                platform_write = BrokerPlatformWriter(
                    container.ads_platform_port, use_cases.proposals
                )
                command = WriteCommand(
                    entity_ref=proposal.diff.entity_ref,
                    parameter=proposal.diff.parameter,
                    before=Money.of("8"),
                    value=Money.of("10"),
                )

                with pytest.raises(PlatformWriteDeniedError) as denied:
                    await platform_write.execute_write(command, authorization, idempotency_key)

                assert denied.value.error_code == "rule_authorization_cannot_increase_spend"
                assert search_client.budget_mutations == []
        finally:
            await container.aclose()
