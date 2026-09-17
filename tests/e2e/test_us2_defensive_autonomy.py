"""US2 end to end (`quickstart.md §6`, tasks.md T074): defensive autonomy
through the REAL cycles -- `LiveRuleStep`/`RuleCycle`, `ExecutionChokepoint`,
a real `ads-broker` on a Unix socket with `GoogleAdsAdapter` and the Google
Ads SDK replaced by an in-memory double (contracts/platform-port.md: "SDK
mocked in tests").

Reuses the broker/adapter wiring from `tests/integration/composition/
test_write_path_end_to_end.py` (the flagship) instead of re-deriving it --
same Ed25519 seed, same `GoogleAdsAdapter` config, same doubled search
client -- so this bank is provably exercising the exact path production
wires (`composition/broker.py::_build_write_pipeline`,
`Container.build_execution_use_cases`). What is NEW here, not covered by
the flagship or by `tests/integration/orchestration/
test_rule_and_execution_cycles.py`: driving `LiveRuleStep` (the real
`RuleCycle` step) all the way to `EXECUTED` against a real broker (the
existing `RuleCycle` bank has no broker and stops at `SKIPPED_DRIFT`), plus
undo (both inside and outside the grace window), the daily
max-changes-per-entity guardrail, the emergency brake, the broker's hard
cap, drift, and a persistence-layer replay guard.

Three bugs surfaced while writing this bank (previously pinned as
`xfail(strict=True)`, now fixed -- see each test's docstring for the exact
evidence): `ExecutionAttempt.previous_value` never set on the real write
path (any AUTO budget decrease violated `spend_ledger`'s CHECK
constraint); `UndoExecution` saving the compensating `Authorization`
before its `Proposal` existed (FK violation misreported as a duplicate);
and `GuardrailEvaluator.evaluate`'s `clamped_after` never applied to what
actually got written.

The fourth gap, `GET /api/v1/executions/{id}`, is wired now
(`execution/presentation/rest.py`) and covered for real in
`tests/integration/composition/test_execution_read_rest.py` (shape, 404,
IDOR), so the routing pin that lived here is gone."""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from sqlalchemy import text

from safent_ads.audit.application.ports import DecisionLogFilter
from safent_ads.audit.application.verify_decision_log_chain import VerifyDecisionLogChain
from safent_ads.audit.domain.chain import ChainVerifier
from safent_ads.audit.domain.entry import DecisionKind
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.broker.infrastructure.adapter_registry import PlatformAdapterRegistry
from safent_ads.broker.infrastructure.caps_config import parse_caps_config
from safent_ads.broker.infrastructure.write_ledger_store import WriteLedgerStore
from safent_ads.broker.platforms.google_ads_adapter import GoogleAdsAdapter, GoogleAdsAdapterConfig
from safent_ads.broker.platforms.write_pipeline import WriteAuthorizationPipeline
from safent_ads.broker.presentation.socket_server import serve
from safent_ads.composition.container import Container
from safent_ads.execution.application.toggle_emergency_brake import EngageBrakeCommand
from safent_ads.execution.application.undo_execution import (
    ExecutionAlreadyUndoneError,
    UndoExecutionCommand,
    UndoOutcome,
)
from safent_ads.execution.domain.execution_attempt import ExecutionAttempt, ExecutionStatus
from safent_ads.execution.domain.guardrails import (
    BrakeMode,
    BrakeScope,
    BrakeScopeKind,
    GuardrailChange,
    GuardrailScope,
    ScopeKind,
    effective_diff,
)
from safent_ads.execution.domain.identifiers import ExecutionId
from safent_ads.execution.infrastructure.errors import DuplicateExecutionError
from safent_ads.execution.infrastructure.sql_execution_queue import SqlExecutionQueue
from safent_ads.execution.infrastructure.sql_spend_ledger import SqlSpendLedger
from safent_ads.orchestration.infrastructure.rule_step import LiveRuleStep
from safent_ads.proposals.application.submit_approval import SubmitApprovalCommand
from safent_ads.proposals.domain.authorization import (
    AuthorizationChannel,
    AuthorizationKind,
    sign_authorization,
)
from safent_ads.proposals.domain.cause import Cause, CauseKey
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.identifiers import AuthorizationId, ProposalId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Priority, Urgency
from safent_ads.proposals.domain.proposal import (
    Proposal,
    ProposedDiff,
    new_proposal_id,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.crypto.ed25519 import ApprovalVerifier
from safent_ads.shared.ids import BusinessId, PlatformCode
from tests.conftest import rolled_back_session
from tests.contracts.execution.conftest import (
    FIRING_RULE_CODE,
    NOW,
    GuardrailLimits,
    calibrate_rule,
    digest,
    seed_authorized_proposal,
    seed_freshness,
    seed_guardrails,
    seed_sell_signal,
)
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.integration.composition.test_write_path_end_to_end import (
    _APPROVAL_PUBLIC_KEY_B64,
    _APPROVAL_SEED_B64,
    _broker_runtime,
    _campaign_row,
    _campaign_state_hash,
    _FakeGoogleSearchClient,
    _running_broker,
)
from tests.unit.broker.ledger_scope_fakes import fake_scope
from tests.unit.composition.factories import build_api_settings

pytestmark = pytest.mark.integration

_SET_BUDGET = text(
    "UPDATE ad_entities SET budget_amount_minor = 10000, budget_currency = 'EUR', "
    "budget_kind = 'daily', platform_state_hash = :state_hash WHERE entity_ref = :entity_ref"
)
_GUARDRAIL_LIMITS = GuardrailLimits(
    daily_cap="500",
    monthly_cap="9000",
    floor="60",
    ceiling="300",
    max_step_pct=0.30,
    max_changes_per_entity_day=2,
)


def _reused_session_factory(session: Any) -> Any:
    """`LiveRuleStep`/`RuleCycle` open their own session per business; this
    bank shares the ONE `rolled_back_session` used for setup so everything
    lands (and rolls back) atomically, same pattern as
    `tests/integration/orchestration/test_rule_and_execution_cycles.py`."""

    @asynccontextmanager
    async def _factory():
        yield session

    return _factory


async def _seed_google_campaign(session: Any, *, customer_id: str, amount_micros: int):
    """Campana Google con presupuesto/hash coherentes con el doble del
    broker: `LiveRuleStep` fija `expected_state_hash` desde
    `AdEntity.platform_state_hash` (no lo recalcula), asi que esa columna
    tiene que coincidir con lo que el broker leera de verdad -- si no,
    todo intento sale `SKIPPED_DRIFT`, nunca `EXECUTED`."""
    entity_ref = campaign_ref(f"customers/{customer_id}/campaigns/1", platform_value="google")
    row = _campaign_row(customer_id=customer_id, campaign_id="1", amount_micros=amount_micros)
    state_hash = _campaign_state_hash(row)
    business_id = await seed_entity(session, entity_ref)
    await session.execute(_SET_BUDGET, {"entity_ref": str(entity_ref), "state_hash": state_hash})
    return entity_ref, business_id, row, state_hash


async def _authorized_broker_container(database_url: str, socket_path: Path) -> Container:
    settings = build_api_settings(
        database_url=database_url,
        broker_socket_path=str(socket_path),
        approval_signing_key=_APPROVAL_SEED_B64,
    )
    container = Container.build(settings)
    container.clock = FixedClock(NOW)
    return container


# ---------------------------------------------------------------------------
# quickstart §6.1-6.4: guardarrailes + regla M05 AUTO + condicion forzada
# (presupuesto bajo) -> RuleCycle real -> ExecutionChokepoint real -> broker
# real -> EXECUTED, con el "recibo automatico" (entrada de decision_log con
# antes/despues) y la cadena de auditoria verificando despues.
#
# BUG corregido, encontrado al escribir este banco:
# `LiveRuleStep._propose_and_authorize` (orchestration/infrastructure/
# rule_step.py) construia el `ExecutionAttempt` de una accion AUTO sin
# `previous_value`. `SqlSpendLedger` (execution/infrastructure/
# sql_spend_ledger.py) deriva `previous_value_minor` de ESE campo, no del
# diff de la propuesta -- sin el, partia de 0, y CUALQUIER bajada de
# presupuesto (el caso central de "autonomia defensiva") dejaba
# `new_value_minor` en negativo, violando el CHECK
# `spend_ledger_new_value_minor_check` (0009_executions.py) en cuanto el
# chokepoint anotaba el cambio aplicado. `SubmitApproval.execute` y
# `UndoExecution._restore_immediately` tenian el MISMO patron (mismo campo
# omitido en construccion) -- arreglado en un unico punto: `Execution
# Chokepoint._process` fija `attempt.previous_value = proposal.diff.before`
# nada mas cargar la propuesta (la misma que el paso 5 revalida contra el
# estado remoto antes de que nada lo use), asi que los tres llamadores no
# necesitan repetir la logica.
# ---------------------------------------------------------------------------


async def test_autonomous_rule_proposes_and_owner_approval_executes(
    isolated_database_url: str, tmp_path: Path
) -> None:
    customer_id = "9200000001"
    search_client = _FakeGoogleSearchClient(
        _campaign_row(customer_id=customer_id, campaign_id="1", amount_micros=100_000_000)
    )
    async with _running_broker(
        tmp_path, search_client=search_client, customer_id=customer_id
    ) as socket_path:
        container = await _authorized_broker_container(isolated_database_url, socket_path)
        try:
            async with rolled_back_session(isolated_database_url) as session:
                entity_ref, business_id, _row, _hash = await _seed_google_campaign(
                    session, customer_id=customer_id, amount_micros=100_000_000
                )
                await seed_freshness(session, entity_ref, lag_minutes=5)
                await seed_sell_signal(session, entity_ref)
                await calibrate_rule(session, enabled=True)
                await seed_guardrails(
                    session,
                    scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref)),
                    limits=_GUARDRAIL_LIMITS,
                    level="business",
                )

                rule_step = LiveRuleStep(
                    _reused_session_factory(session),
                    container.build_execution_use_cases,
                    container.clock,
                )
                await rule_step.run(BusinessId(business_id), "us2-auto-receipt", NOW)

                use_cases = container.build_execution_use_cases(session)
                proposal_state = (
                    await session.execute(
                        text("SELECT state FROM proposals WHERE business_id = :id"),
                        {"id": business_id},
                    )
                ).scalar_one()
                assert proposal_state == "pending", "El agente propone; el propietario decide"
                assert await use_cases.chokepoint.run_once() is None
                assert search_client.budget_mutations == []
                row = (
                    await session.execute(
                        text(
                            "SELECT id AS proposal_id, diff_hash FROM proposals "
                            "WHERE business_id = :id"
                        ),
                        {"id": business_id},
                    )
                ).one()
                approval = await use_cases.submit_approval.execute(
                    SubmitApprovalCommand(
                        proposal_id=ProposalId.parse(str(row.proposal_id)),
                        diff_hash=row.diff_hash,
                        approved_by="owner-qa",
                        channel=AuthorizationChannel.PANEL,
                    )
                )

                # Solo tras la aprobacion del propietario y su periodo de
                # gracia se permite procesar la ejecucion.
                container.clock.advance_to(approval.execution_scheduled_at)  # type: ignore[attr-defined]
                outcome = await use_cases.chokepoint.run_once()

                assert outcome is ExecutionStatus.EXECUTED
                assert len(search_client.budget_mutations) == 1
                assert search_client.budget_mutations[0][2] == 70_000_000  # 100 -> 70 (SELL 30%)

                decision_log = await SqlDecisionLogRepository(session).search(
                    DecisionLogFilter(
                        business_id=BusinessId(business_id), kind=DecisionKind.EXECUTION
                    )
                )
                # >=1, no ==1, igual que test_write_path_end_to_end.py: `_persist`
                # anexa un `ExecutionAttemptRecorded` en CADA llamada (tambien en
                # la transicion a `EXECUTING`, antes de hablar con la
                # plataforma) mas el `ProposalExecuted` del propio agregado --
                # `entries[0]` (ORDER BY seq DESC) es siempre el ultimo, el
                # recibo con el desenlace final.
                assert len(decision_log.entries) >= 1
                receipt = decision_log.entries[0].payload
                assert receipt["outcome"] == "executed", "el recibo automatico exige el desenlace"

                report = await VerifyDecisionLogChain(
                    SqlDecisionLogRepository(session), ChainVerifier(), container.clock
                ).execute()
                assert report.chain_ok is True
        finally:
            await container.aclose()


# ---------------------------------------------------------------------------
# quickstart §6.5: deshacer DENTRO de la ventana de gracia restaura el
# presupuesto original -- una segunda escritura real a traves del mismo
# broker.
#
# BUG corregido, encontrado al escribir este banco:
# `UndoExecution._restore_immediately` (execution/application/
# undo_execution.py) llamaba `self._authorizations.save(authorization)`
# ANTES que `self._proposals.save(restore_proposal)` -- pero
# `approvals.proposal_id` tiene una FK a `proposals.id`
# (0008_proposals.py), asi que ese INSERT violaba `approvals_proposal_id_
# fkey` contra Postgres real: la propuesta compensatoria todavia no existia
# cuando se firmaba su autorizacion. `SqlAuthorizationRepository._translate`
# (proposals/infrastructure/sql_authorization_repository.py) agravaba el
# diagnostico: su `except` no reconocia el `FOREIGN KEY violation` y lo
# reclasificaba como `DuplicateAuthorizationError`, ocultando la causa
# real -- ahora surge como `AuthorizationProposalNotFoundError`, un tipo
# propio.
# ---------------------------------------------------------------------------


# El recorte de guardarraíl (T3, ya corregido) ahora se APLICA de verdad, y
# el "deshacer" pasa por el MISMO chokepoint que cualquier otra ejecucion --
# nunca es un atajo para saltarse el salto maximo (si lo fuera, "deshacer"
# seria una puerta trasera para subir gasto por encima del limite). La
# restauracion de esta prueba es un salto de +42,86% (70 -> 100): con el
# `max_step_pct=0.30` compartido del resto del banco quedaria recortada a
# 91 -- correcto, pero no es lo que este contrato (quickstart.md §6.5)
# quiere demostrar. Limites LOCALES, mas anchos solo en `max_step_pct`,
# para que la vuelta completa a 100 quepa en un unico paso.
_UNDO_GUARDRAIL_LIMITS = GuardrailLimits(
    daily_cap="500",
    monthly_cap="9000",
    floor="60",
    ceiling="300",
    max_step_pct=0.60,
    max_changes_per_entity_day=2,
)


async def test_undo_within_grace_restores_the_original_budget(
    isolated_database_url: str, tmp_path: Path
) -> None:
    customer_id = "9200000002"
    search_client = _FakeGoogleSearchClient(
        _campaign_row(customer_id=customer_id, campaign_id="1", amount_micros=100_000_000)
    )
    async with _running_broker(
        tmp_path, search_client=search_client, customer_id=customer_id
    ) as socket_path:
        container = await _authorized_broker_container(isolated_database_url, socket_path)
        try:
            async with rolled_back_session(isolated_database_url) as session:
                entity_ref, business_id, _row, expected_hash = await _seed_google_campaign(
                    session, customer_id=customer_id, amount_micros=100_000_000
                )
                await seed_freshness(session, entity_ref, lag_minutes=5)
                await seed_sell_signal(session, entity_ref)
                await calibrate_rule(session, enabled=True)
                await seed_guardrails(
                    session,
                    scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref)),
                    limits=_UNDO_GUARDRAIL_LIMITS,
                    level="business",
                )
                use_cases = container.build_execution_use_cases(session)
                proposal = Proposal.raise_proposal(
                    proposal_id=new_proposal_id(),
                    business_id=BusinessId(business_id),
                    diff=ProposedDiff.build(
                        entity_ref=entity_ref,
                        parameter="daily_budget",
                        before=Money.of("100"),
                        after=Money.of("70"),
                    ),
                    classification=Classification.ROUTINE,
                    cause=Cause(
                        text="ROAS por debajo del objetivo en 7D", rule_id=FIRING_RULE_CODE
                    ),
                    cause_key=CauseKey(
                        entity_ref=entity_ref, rule_id=FIRING_RULE_CODE, cause_type="roas_low"
                    ),
                    evidence=(),
                    estimated_impact=Money.of("310"),
                    priority=Priority(urgency=Urgency.RECOMMENDED),
                    now=NOW,
                    expires_at=NOW.replace(hour=23),
                    expected_state_hash=expected_hash,
                )
                await use_cases.proposals.save(proposal)
                approval = await use_cases.submit_approval.execute(
                    SubmitApprovalCommand(
                        proposal_id=proposal.proposal_id,
                        diff_hash=proposal.diff.diff_hash,
                        approved_by="owner-qa",
                        channel=AuthorizationChannel.PANEL,
                    )
                )
                original_execution_id = ExecutionId(uuid.UUID(approval.execution_id))
                container.clock.advance_to(approval.execution_scheduled_at)  # type: ignore[attr-defined]
                outcome = await use_cases.chokepoint.run_once()
                assert outcome is ExecutionStatus.EXECUTED

                # Deshacer DENTRO de la gracia (por defecto, ventana amplia
                # -- UndoGracePolicy): "Deshacer" ES la autorizacion humana
                # explicita, nunca una rule_authorization (FR-11 en accion
                # inversa).
                undo_result = await use_cases.undo_execution.execute(
                    UndoExecutionCommand(proposal_id=proposal.proposal_id, initiated_by="qa-e2e")
                )
                assert undo_result.outcome is UndoOutcome.RESTORED

                # BUG corregido (esta rama): `executions.undone_at`/
                # `compensating_proposal_id` nunca se escribian -- el panel
                # no podia mostrar "deshecha" y nada impedia deshacer dos
                # veces la misma ejecucion.
                marked_row = (
                    await session.execute(
                        text(
                            "SELECT undone_at, compensating_proposal_id FROM executions "
                            "WHERE id = :id"
                        ),
                        {"id": str(original_execution_id)},
                    )
                ).one()
                assert marked_row.undone_at is not None
                assert str(marked_row.compensating_proposal_id) == str(
                    undo_result.compensating_proposal_id
                )

                # Solo-anexable: un segundo "Deshacer" sobre la MISMA
                # ejecucion es un conflicto tipado, nunca una segunda
                # propuesta compensatoria.
                with pytest.raises(ExecutionAlreadyUndoneError):
                    await use_cases.undo_execution.execute(
                        UndoExecutionCommand(
                            proposal_id=proposal.proposal_id, initiated_by="qa-e2e"
                        )
                    )

                # La restauracion queda agendada de inmediato (grace=0): un
                # segundo `run_once()` la reclama y la ejecuta.
                restore_outcome = await use_cases.chokepoint.run_once()
                assert restore_outcome is ExecutionStatus.EXECUTED
                assert len(search_client.budget_mutations) == 2
                assert search_client.budget_mutations[0][2] == 70_000_000
                assert search_client.budget_mutations[1][2] == 100_000_000, (
                    "el segundo apunte debe restaurar el presupuesto original (100)"
                )
        finally:
            await container.aclose()


# ---------------------------------------------------------------------------
# quickstart §6.5 (segunda mitad): deshacer FUERA de la ventana de gracia
# crea una propuesta compensatoria `pendiente`, sujeta al flujo normal de
# aprobacion -- a diferencia de la restauracion inmediata,
# `_create_compensating_proposal` no toca `executions`/`approvals`, asi que
# no pisa el bug de arriba.
# ---------------------------------------------------------------------------


async def test_undo_after_grace_creates_a_pending_compensating_proposal(
    isolated_database_url: str, tmp_path: Path
) -> None:
    customer_id = "9200000004"
    search_client = _FakeGoogleSearchClient(
        _campaign_row(customer_id=customer_id, campaign_id="1", amount_micros=100_000_000)
    )
    async with _running_broker(
        tmp_path, search_client=search_client, customer_id=customer_id
    ) as socket_path:
        container = await _authorized_broker_container(isolated_database_url, socket_path)
        try:
            async with rolled_back_session(isolated_database_url) as session:
                entity_ref, business_id, _row, expected_hash = await _seed_google_campaign(
                    session, customer_id=customer_id, amount_micros=100_000_000
                )
                await seed_guardrails(
                    session,
                    scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref)),
                    limits=_GUARDRAIL_LIMITS,
                    level="business",
                )
                use_cases = container.build_execution_use_cases(session)
                proposal = Proposal.raise_proposal(
                    proposal_id=new_proposal_id(),
                    business_id=BusinessId(business_id),
                    diff=ProposedDiff.build(
                        entity_ref=entity_ref,
                        parameter="daily_budget",
                        before=Money.of("100"),
                        after=Money.of("70"),
                    ),
                    classification=Classification.ROUTINE,
                    cause=Cause(
                        text="ROAS por debajo del objetivo en 7D", rule_id=FIRING_RULE_CODE
                    ),
                    cause_key=CauseKey(
                        entity_ref=entity_ref, rule_id=FIRING_RULE_CODE, cause_type="roas_low"
                    ),
                    evidence=(),
                    estimated_impact=Money.of("310"),
                    priority=Priority(urgency=Urgency.RECOMMENDED),
                    now=NOW,
                    expires_at=NOW.replace(hour=23),
                    expected_state_hash=expected_hash,
                )
                await use_cases.proposals.save(proposal)
                scope = GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref))
                guardrails = await use_cases.guardrail_sets.get_effective(scope)
                spend_ledger = SqlSpendLedger(session, container.clock, use_cases.execution_queue)
                ledger_snapshot = await spend_ledger.snapshot(scope, entity_ref)
                verdict = use_cases.guardrail_evaluator.evaluate(
                    GuardrailChange(
                        scope=scope,
                        entity_ref=entity_ref,
                        authorization_kind=AuthorizationKind.HUMAN_APPROVAL,
                        before=Money.of("100"),
                        after=Money.of("70"),
                    ),
                    guardrails,
                    ledger_snapshot,
                )
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
                await use_cases.execution_queue.save(
                    ExecutionAttempt(
                        execution_id=ExecutionId.new(),
                        business_id=proposal.business_id,
                        proposal_id=proposal.proposal_id,
                        authorization_id=authorization.authorization_id,
                        idempotency_key=f"exec-{proposal.proposal_id}-{proposal.diff.diff_hash[:12]}",
                        previous_value=Money.of("100"),
                        platform_state_hash_before=expected_hash,
                    )
                )
                outcome = await use_cases.chokepoint.run_once()
                assert outcome is ExecutionStatus.EXECUTED

                # Fuera de la gracia de 30 min (`UndoGracePolicy.
                # budget_decrease_grace`).
                container.clock.advance_to(NOW + timedelta(minutes=31))  # type: ignore[attr-defined]
                undo_result = await use_cases.undo_execution.execute(
                    UndoExecutionCommand(proposal_id=proposal.proposal_id, initiated_by="qa-e2e")
                )
                assert undo_result.outcome is UndoOutcome.COMPENSATING_PROPOSAL_CREATED

                compensating_rows = (
                    (
                        await session.execute(
                            text(
                                "SELECT state, proposed_value FROM proposals "
                                "WHERE business_id = :id AND id <> :original ORDER BY created_at"
                            ),
                            {"id": business_id, "original": str(proposal.proposal_id)},
                        )
                    )
                    .mappings()
                    .all()
                )
                assert len(compensating_rows) == 1
                assert compensating_rows[0]["state"] == "pending", (
                    "la compensatoria debe quedar pendiente, sujeta a aprobacion normal"
                )
                # Ningun mutation nuevo: la compensatoria todavia no se aprobo.
                assert len(search_client.budget_mutations) == 1
        finally:
            await container.aclose()


async def _human_approval_execution(
    container: Container,
    session: Any,
    use_cases: Any,
    *,
    entity_ref,
    business_id: uuid.UUID,
    before: str,
    after: str,
    expected_hash: str,
) -> ExecutionStatus:
    """Camino `human_approval` completo (propuesta -> autorizacion firmada
    -> agendada -> `chokepoint.run_once()`), con `previous_value` fijado a
    mano -- ver el docstring de la primera prueba de este banco sobre por
    que `SubmitApproval.execute` real no puede usarse aqui sin heredar el
    mismo bug."""
    scope = GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref))
    proposal = Proposal.raise_proposal(
        proposal_id=new_proposal_id(),
        business_id=BusinessId(business_id),
        diff=ProposedDiff.build(
            entity_ref=entity_ref,
            parameter="daily_budget",
            before=Money.of(before),
            after=Money.of(after),
        ),
        classification=Classification.ROUTINE,
        cause=Cause(text="Contrato QA", rule_id=None),
        cause_key=CauseKey(entity_ref=entity_ref, rule_id="qa", cause_type="test"),
        evidence=(),
        estimated_impact=Money.of("10"),
        priority=Priority(urgency=Urgency.RECOMMENDED),
        now=container.clock.now(),
        expires_at=container.clock.now() + timedelta(hours=1),
        expected_state_hash=expected_hash,
    )
    await use_cases.proposals.save(proposal)
    guardrails = await use_cases.guardrail_sets.get_effective(scope)
    spend_ledger = SqlSpendLedger(session, container.clock, use_cases.execution_queue)
    ledger_snapshot = await spend_ledger.snapshot(scope, entity_ref)
    verdict = use_cases.guardrail_evaluator.evaluate(
        GuardrailChange(
            scope=scope,
            entity_ref=entity_ref,
            authorization_kind=AuthorizationKind.HUMAN_APPROVAL,
            before=Money.of(before),
            after=Money.of(after),
        ),
        guardrails,
        ledger_snapshot,
    )
    # `effective_diff`, no `proposal.diff.diff_hash` a secas: mismo patron
    # que `SubmitApproval.execute` real (ver su docstring) -- si el
    # guardarraíl recorta, la autorizacion firma el diff RECORTADO
    # (`effective_diff` ya devuelve el diff sin recortar cuando `not
    # verdict.allowed`: no hay diff recortado real que firmar).
    authorization = sign_authorization(
        authorization_id=AuthorizationId.new(),
        proposal_id=proposal.proposal_id,
        kind=AuthorizationKind.HUMAN_APPROVAL,
        proposal_classification=proposal.classification,
        diff_hash=effective_diff(proposal.diff, verdict).diff_hash,
        guardrail_verdict_hash=verdict.verdict_hash,
        issued_by="owner-de-contrato",
        channel=AuthorizationChannel.PANEL,
        decided_at=container.clock.now(),
        expires_at=container.clock.now() + timedelta(hours=1),
        signer=container.approval_key_pair.signer,
    )
    await use_cases.authorizations.save(authorization)
    proposal.approve(proposal.diff.diff_hash, container.clock.now())
    await use_cases.proposals.save(proposal)
    proposal.schedule_execution(0, container.clock.now())
    await use_cases.proposals.save(proposal)
    await use_cases.execution_queue.save(
        ExecutionAttempt(
            execution_id=ExecutionId.new(),
            business_id=proposal.business_id,
            proposal_id=proposal.proposal_id,
            authorization_id=authorization.authorization_id,
            idempotency_key=f"exec-{proposal.proposal_id}-{proposal.diff.diff_hash[:12]}",
            previous_value=Money.of(before),
            platform_state_hash_before=expected_hash,
        )
    )
    return await use_cases.chokepoint.run_once()


# ---------------------------------------------------------------------------
# quickstart §6.6: el suelo de guardarraíl nunca se cruza -- bajar una
# campana ya en 65 EUR/dia con suelo=60 debe recortarse a 60, avisando.
#
# BUG corregido, encontrado al escribir este banco:
# `GuardrailEvaluator.evaluate` (execution/domain/guardrails.py) calcula
# `clamped_after` (recorte a suelo/techo/salto maximo) y lo devuelve en
# `GuardrailVerdict.clamped_after` con `allowed=True` -- pero nada en la
# capa de aplicacion consumia ese valor: `ExecutionChokepoint._execute`/
# `_build_write_command` construia el `WriteCommand` desde
# `proposal.diff.after`, tal cual, ignorando el veredicto.
#
# Semantica decidida (spec.md/threat-model C-15/C-17): el valor ESCRITO es
# el recortado; `proposal.diff` NUNCA se muta (INV-1: una autorizacion
# firma un unico `diff_hash` concreto, y sigue siendo auditable lo que se
# pidio de verdad, sin recortar) -- `AuthorizeRuleAction`/`SubmitApproval`/
# `UndoExecution` firman el diff EFECTIVO (`execution.domain.guardrails.
# effective_diff`: el mismo diff si no hay recorte, o uno con `after`
# recortado si lo hay) y `ExecutionChokepoint` verifica y escribe ese mismo
# diff efectivo -- si divergiesen, la reverificacion independiente del
# bróker (que recalcula el `diff_hash` desde el valor que de verdad viaja
# en el `WriteCommand`) denegaria por `diff_hash_mismatch`. Si el recorte
# deja el cambio en NO-OP (ya estamos en el limite), la ejecucion se
# completa como `BLOCKED_GUARDRAIL` con el motivo `guardrail_clamp_is_noop`
# -- `executions.outcome` es un CHECK cerrado en Postgres sin hueco para un
# estado nuevo sin migracion, y `BLOCKED_GUARDRAIL` es el desenlace
# existente que mejor encaja (nada se escribe, el guardarraíl es la razon).
# ---------------------------------------------------------------------------


async def test_floor_guardrail_clamps_the_written_budget(
    isolated_database_url: str, tmp_path: Path
) -> None:
    customer_id = "9200000005"
    search_client = _FakeGoogleSearchClient(
        _campaign_row(customer_id=customer_id, campaign_id="1", amount_micros=65_000_000)
    )
    async with _running_broker(
        tmp_path, search_client=search_client, customer_id=customer_id
    ) as socket_path:
        container = await _authorized_broker_container(isolated_database_url, socket_path)
        try:
            async with rolled_back_session(isolated_database_url) as session:
                entity_ref, business_id, _row, expected_hash = await _seed_google_campaign(
                    session, customer_id=customer_id, amount_micros=65_000_000
                )
                await seed_guardrails(
                    session,
                    scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref)),
                    limits=_GUARDRAIL_LIMITS,  # floor=60
                    level="business",
                )
                use_cases = container.build_execution_use_cases(session)
                outcome = await _human_approval_execution(
                    container,
                    session,
                    use_cases,
                    entity_ref=entity_ref,
                    business_id=business_id,
                    before="65",
                    after="50",
                    expected_hash=expected_hash,
                )
                assert outcome is ExecutionStatus.EXECUTED

                written_amount_micros = search_client.budget_mutations[0][2]
                assert written_amount_micros == 60_000_000, (
                    "el presupuesto escrito nunca debe cruzar el suelo del guardarraíl (60)"
                )
        finally:
            await container.aclose()


# ---------------------------------------------------------------------------
# quickstart §6.7/§6.9 (guardarraíl de aplicacion): un tercer cambio el
# mismo dia sobre la misma entidad se difiere con un motivo explicito
# (max_changes_per_entity_day_reached), aunque cada cambio individual
# respete suelo/techo/salto.
# ---------------------------------------------------------------------------


async def test_third_same_day_change_is_blocked_with_an_explicit_reason(
    isolated_database_url: str, tmp_path: Path
) -> None:
    customer_id = "9200000006"
    search_client = _FakeGoogleSearchClient(
        _campaign_row(customer_id=customer_id, campaign_id="1", amount_micros=100_000_000)
    )
    async with _running_broker(
        tmp_path, search_client=search_client, customer_id=customer_id
    ) as socket_path:
        container = await _authorized_broker_container(isolated_database_url, socket_path)
        try:
            async with rolled_back_session(isolated_database_url) as session:
                entity_ref, business_id, _row, expected_hash = await _seed_google_campaign(
                    session, customer_id=customer_id, amount_micros=100_000_000
                )
                await seed_guardrails(
                    session,
                    scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref)),
                    limits=_GUARDRAIL_LIMITS,  # max_changes_per_entity_day=2
                    level="business",
                )
                use_cases = container.build_execution_use_cases(session)

                first = await _human_approval_execution(
                    container,
                    session,
                    use_cases,
                    entity_ref=entity_ref,
                    business_id=business_id,
                    before="100",
                    after="90",
                    expected_hash=expected_hash,
                )
                second = await _human_approval_execution(
                    container,
                    session,
                    use_cases,
                    entity_ref=entity_ref,
                    business_id=business_id,
                    before="100",
                    after="85",
                    expected_hash=expected_hash,
                )
                third = await _human_approval_execution(
                    container,
                    session,
                    use_cases,
                    entity_ref=entity_ref,
                    business_id=business_id,
                    before="100",
                    after="80",
                    expected_hash=expected_hash,
                )

                assert (first, second) == (ExecutionStatus.EXECUTED, ExecutionStatus.EXECUTED)
                assert third is ExecutionStatus.BLOCKED_GUARDRAIL

                decision_log = await SqlDecisionLogRepository(session).search(
                    DecisionLogFilter(
                        business_id=BusinessId(business_id), kind=DecisionKind.EXECUTION
                    )
                )
                last_entry = sorted(decision_log.entries, key=lambda e: e.seq)[-1]
                assert last_entry.payload["error_code"] == "max_changes_per_entity_day_reached"
        finally:
            await container.aclose()


# ---------------------------------------------------------------------------
# quickstart §6.8: freno de emergencia en modo AUTONOMOUS -> la siguiente
# senal accionable (via RuleCycle REAL) produce propuesta pendiente, NUNCA
# ejecucion; una aprobacion HUMANA sigue adelante bajo el mismo freno
# (`EmergencyBrake.blocks`: `AUTONOMOUS` solo frena `rule_authorization`).
# ---------------------------------------------------------------------------


async def test_autonomous_brake_blocks_rule_actions_but_not_human_approvals(
    isolated_database_url: str, tmp_path: Path
) -> None:
    customer_id = "9200000007"
    search_client = _FakeGoogleSearchClient(
        _campaign_row(customer_id=customer_id, campaign_id="1", amount_micros=100_000_000)
    )
    async with _running_broker(
        tmp_path, search_client=search_client, customer_id=customer_id
    ) as socket_path:
        container = await _authorized_broker_container(isolated_database_url, socket_path)
        try:
            async with rolled_back_session(isolated_database_url) as session:
                entity_ref, business_id, _row, expected_hash = await _seed_google_campaign(
                    session, customer_id=customer_id, amount_micros=100_000_000
                )
                await seed_freshness(session, entity_ref, lag_minutes=5)
                await seed_sell_signal(session, entity_ref)
                await calibrate_rule(session, enabled=True)
                await seed_guardrails(
                    session,
                    scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref)),
                    limits=_GUARDRAIL_LIMITS,
                    level="business",
                )
                use_cases = container.build_execution_use_cases(session)
                brake_scope = BrakeScope(
                    kind=BrakeScopeKind.PLATFORM_ACCOUNT,
                    ref=str(entity_ref),
                )
                await use_cases.toggle_emergency_brake.engage(
                    EngageBrakeCommand(
                        business_id=BusinessId(business_id),
                        scope=brake_scope,
                        mode=BrakeMode.AUTONOMOUS,
                        reason="prueba de contrato",
                    )
                )

                rule_step = LiveRuleStep(
                    _reused_session_factory(session),
                    container.build_execution_use_cases,
                    container.clock,
                )
                await rule_step.run(BusinessId(business_id), "us2-brake", NOW)

                proposal_state = (
                    await session.execute(
                        text("SELECT state FROM proposals WHERE business_id = :id"),
                        {"id": business_id},
                    )
                ).scalar_one()
                assert proposal_state == "pending", (
                    "con el freno AUTONOMOUS activo, la senal produce propuesta, no ejecucion"
                )
                assert search_client.budget_mutations == [], (
                    "nada debe ejecutarse con el freno puesto"
                )

                # `ix_proposals_open_per_parameter` (0008_proposals.py) solo
                # admite UNA propuesta abierta por entidad+parametro: la
                # propuesta pendiente que dejo el RuleCycle denegado se
                # rechaza (una decision real del propietario) antes de la
                # aprobacion humana de abajo, sobre el MISMO parametro.
                pending = await use_cases.proposals.get(
                    ProposalId.parse(
                        str(
                            (
                                await session.execute(
                                    text("SELECT id FROM proposals WHERE business_id = :id"),
                                    {"id": business_id},
                                )
                            ).scalar_one()
                        )
                    )
                )
                assert pending is not None
                pending.reject(
                    container.clock.now(), "liberado para la prueba de aprobacion humana"
                )
                await use_cases.proposals.save(pending)

                # Una aprobacion HUMANA, bajo el MISMO freno, sigue adelante.
                human_outcome = await _human_approval_execution(
                    container,
                    session,
                    use_cases,
                    entity_ref=entity_ref,
                    business_id=business_id,
                    before="100",
                    after="90",
                    expected_hash=expected_hash,
                )
                assert human_outcome is ExecutionStatus.EXECUTED
        finally:
            await container.aclose()


def _tight_caps_yaml(customer_id: str) -> str:
    """A diferencia de `_caps_yaml` (flagship, generosos a proposito), un
    `floor_minor` del bróker MAS ESTRECHO que el suelo del guardarraíl de
    aplicacion (`_GUARDRAIL_LIMITS.floor = 60`) fuerza `FLOOR_EXCEEDED` en
    el PRIMER intento (`_check_bounds`, broker/domain/write_authorization.py,
    se evalua ANTES que la deriva) -- prueba especificamente que el tope
    del bróker deniega aunque el guardarraíl de aplicacion lo hubiera
    permitido (C-17, defensa en profundidad)."""
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
            floor_minor: 9500
            ceiling_minor: 100000000
        """
    )


@asynccontextmanager
async def _running_broker_with_tight_caps(
    tmp_path: Path, *, search_client: _FakeGoogleSearchClient, customer_id: str
):
    """Mismo cableado que `_running_broker` del flagship, con un
    `caps.yaml` deliberadamente estrecho en vez del generoso por defecto
    -- la unica forma de probar el tope duro del bróker (quickstart.md
    §6.9) sin reescribir `_running_broker` para aceptar el YAML como
    parametro."""
    socket_path = tmp_path / "broker.sock"
    verifier = ApprovalVerifier.from_public_key_b64(_APPROVAL_PUBLIC_KEY_B64)
    caps = parse_caps_config(_tight_caps_yaml(customer_id))
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


# ---------------------------------------------------------------------------
# quickstart §6.9: el tope duro del bróker deniega aunque el guardarraíl de
# APLICACION lo hubiera permitido (defensa en profundidad, C-17) -- cero
# intentos por encima del tope duro, ninguna mutacion en el SDK doblado.
# ---------------------------------------------------------------------------


async def test_broker_hard_cap_blocks_even_when_the_application_guardrail_allows(
    isolated_database_url: str, tmp_path: Path
) -> None:
    customer_id = "9200000008"
    search_client = _FakeGoogleSearchClient(
        _campaign_row(customer_id=customer_id, campaign_id="1", amount_micros=100_000_000)
    )
    async with _running_broker_with_tight_caps(
        tmp_path, search_client=search_client, customer_id=customer_id
    ) as socket_path:
        container = await _authorized_broker_container(isolated_database_url, socket_path)
        try:
            async with rolled_back_session(isolated_database_url) as session:
                entity_ref, business_id, _row, expected_hash = await _seed_google_campaign(
                    session, customer_id=customer_id, amount_micros=100_000_000
                )
                await seed_guardrails(
                    session,
                    scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref)),
                    limits=_GUARDRAIL_LIMITS,  # guardarraíl de aplicacion generoso: lo permitiria
                    level="business",
                )
                use_cases = container.build_execution_use_cases(session)
                outcome = await _human_approval_execution(
                    container,
                    session,
                    use_cases,
                    entity_ref=entity_ref,
                    business_id=business_id,
                    before="100",
                    after="90",
                    expected_hash=expected_hash,
                )

                assert outcome is ExecutionStatus.FAILED, (
                    "el tope duro del broker debe denegar aunque la app lo permitiera"
                )
                assert search_client.budget_mutations == [], (
                    "cero intentos por encima del tope duro"
                )

                decision_log = await SqlDecisionLogRepository(session).search(
                    DecisionLogFilter(
                        business_id=BusinessId(business_id), kind=DecisionKind.EXECUTION
                    )
                )
                last_entry = sorted(decision_log.entries, key=lambda e: e.seq)[-1]
                # ADS-01 (b7dc28b) separo `ConfirmedWriteRejection` (rechazo
                # verificado del broker ANTES de mutar: DENIED/BLOCKED_HARD_CAP/
                # SKIPPED_DRIFT) del error generico de transporte que ahora deja
                # el intento en UNKNOWN en vez de FAILED (no prueba ausencia de
                # efecto remoto). El chokepoint ya no envuelve el rechazo
                # confirmado en `platform_write_error:<clase>` -- registra
                # `str(exc)` tal cual, que para `PlatformWriteRejectedError` es
                # `"<outcome>: <error_code>"` (errors.py). Mas trazable: el
                # tope y el motivo exactos, no solo el nombre de la clase.
                assert last_entry.payload["error_code"] == "BLOCKED_HARD_CAP: floor_exceeded"
        finally:
            await container.aclose()


# ---------------------------------------------------------------------------
# quickstart §6 (deriva): el estado remoto declarado por la propuesta
# (`expected_state_hash`) ya no coincide con lo que el bróker lee de
# verdad -> `SKIPPED_DRIFT`, nunca `EXECUTED` (plan.md §6, paso 5).
# ---------------------------------------------------------------------------


async def test_drift_between_expected_and_remote_state_skips_execution(
    isolated_database_url: str, tmp_path: Path
) -> None:
    customer_id = "9200000009"
    search_client = _FakeGoogleSearchClient(
        _campaign_row(customer_id=customer_id, campaign_id="1", amount_micros=100_000_000)
    )
    async with _running_broker(
        tmp_path, search_client=search_client, customer_id=customer_id
    ) as socket_path:
        container = await _authorized_broker_container(isolated_database_url, socket_path)
        try:
            async with rolled_back_session(isolated_database_url) as session:
                entity_ref, business_id, _row, _correct_hash = await _seed_google_campaign(
                    session, customer_id=customer_id, amount_micros=100_000_000
                )
                await seed_guardrails(
                    session,
                    scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref)),
                    limits=_GUARDRAIL_LIMITS,
                    level="business",
                )
                use_cases = container.build_execution_use_cases(session)
                stale_hash = digest("un-estado-que-ya-no-es-el-remoto")
                outcome = await _human_approval_execution(
                    container,
                    session,
                    use_cases,
                    entity_ref=entity_ref,
                    business_id=business_id,
                    before="100",
                    after="90",
                    expected_hash=stale_hash,
                )

                assert outcome is ExecutionStatus.SKIPPED_DRIFT
                assert search_client.budget_mutations == []

                proposal_state = (
                    await session.execute(
                        text("SELECT state FROM proposals WHERE business_id = :id"),
                        {"id": business_id},
                    )
                ).scalar_one()
                assert proposal_state == "invalidated"
        finally:
            await container.aclose()


# ---------------------------------------------------------------------------
# Repeticion: un `ExecutionAttempt` con la MISMA `idempotency_key` que uno
# ya persistido nunca duplica el cambio -- guarda de repeticion en la capa
# de PERSISTENCIA (`executions_idempotency_key_unique`), complementaria a
# la del bróker que ya cubre `test_write_path_end_to_end.py`. Es el
# mecanismo exacto que el banco de caos usa para probar "sin cambio
# duplicado" tras matar el worker a media ejecucion.
# ---------------------------------------------------------------------------


async def test_replay_with_the_same_idempotency_key_is_rejected_at_the_queue(
    isolated_database_url: str,
) -> None:
    async with rolled_back_session(isolated_database_url) as session:
        context = await seed_authorized_proposal(session)
        queue = SqlExecutionQueue(session, FixedClock(NOW))
        idempotency_key = f"exec-{context.proposal_id}-{context.diff_hash[:12]}"

        await queue.save(
            ExecutionAttempt(
                execution_id=ExecutionId.new(),
                business_id=context.business_id,
                proposal_id=context.proposal_id,
                authorization_id=context.authorization_id,
                idempotency_key=idempotency_key,
                previous_value=Money.of("100"),
                platform_state_hash_before=context.expected_state_hash,
            )
        )

        with pytest.raises(DuplicateExecutionError):
            await queue.save(
                ExecutionAttempt(
                    execution_id=ExecutionId.new(),
                    business_id=context.business_id,
                    proposal_id=context.proposal_id,
                    authorization_id=context.authorization_id,
                    idempotency_key=idempotency_key,
                    previous_value=Money.of("100"),
                    platform_state_hash_before=context.expected_state_hash,
                )
            )


# ---------------------------------------------------------------------------
# quickstart §6.5/§7.3: `GET /api/v1/executions/{id}` (contracts/rest-api.md
# linea 193) no esta cableado -- `composition/execution_rest.py` implementa
# listar/aprobar/rechazar/deshacer propuestas y el freno, pero ninguna ruta
# de LECTURA de ejecuciones (ni `/executions/{id}` ni `/executions`); su
# propio docstring (lineas 10-23) documenta el resto del alcance reducido
# pero no menciona esta ausencia.
# ---------------------------------------------------------------------------


def _stub_container_for_routing() -> Container:
    settings = build_api_settings(database_url="postgresql+asyncpg://ads:test@localhost/ads_test")
    return Container.build(settings)
