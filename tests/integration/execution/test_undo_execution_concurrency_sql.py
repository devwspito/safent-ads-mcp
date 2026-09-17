"""Cierra security-review-f4.md B-2 (CWE-367): `UndoExecution._undo_executed`
leia `executions.undone_at` con un `SELECT` sin bloqueo y marcaba el
deshacer con un UPSERT incondicional al final -- dos `POST /executions/undo`
simultaneos sobre el MISMO `execution_id` (doble clic del panel, o panel y
`[Deshacer]` de Telegram a la vez) leian los dos `undone_at IS NULL` y los
dos fabricaban su propia propuesta compensatoria; solo una quedaba
referenciada en `executions.compensating_proposal_id`, la otra quedaba
huerfana y el `decision_log` perdia el rastro de una.

El fix, en dos capas, ambas ejercitadas aqui a la vez (mismo criterio que
`test_account_lock_prevents_cap_overshoot_sql.py`):

1. `SqlUnitOfWork.lock_account` PRIMERO -- serializa las dos transacciones
   sobre la MISMA cuenta, igual que `ExecutionChokepoint._pass_gates`.
2. `ExecutionQueuePort.mark_undone_if_pending` -- `UPDATE ... WHERE
   undone_at IS NULL`, atomico -- es el guard real: aunque el candado de
   arriba fuese el unico mecanismo, esta escritura condicional es la que
   demuestra que la segunda transaccion pierde en la base, no solo en
   memoria.

Escenario FUERA de la ventana de gracia (`undo_deadline` en el pasado) a
proposito: es el camino mas simple de `UndoExecution`
(`_create_compensating_proposal`, sin evaluar guardarrailes ni firmar una
autorizacion) y es exactamente el que la revision describe como el dano
observable: "fuera de ella quedan dos propuestas PENDING identicas".

La propuesta se levanta por la via del dominio (`Proposal.raise_proposal` +
`SqlProposalRepository.save()`, un salto de estado por escritura -- PENDING
-> APPROVED -> SCHEDULED -> EXECUTING -> EXECUTED, igual que
`proposals_guard_diff_hash` de 0008_proposals exige), NO con el atajo SQL
crudo de `tests/contracts/execution/conftest.py::seed_proposal`: ese atajo
deja `diff_hash`/`evidence` sin la forma que `SqlProposalRepository.get()`
exige (`ProposalIntegrityError`/`KeyError: 'cause_key'`), y `UndoExecution`
SI pasa por esa lectura -- a diferencia de los demas casos de este
directorio, que solo tocan `executions`/`spend_ledger` a traves de
`ExecutionQueuePort`. Gap preexistente del fixture, ajeno a B-2 (ver
informe de la rama)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from safent_ads.execution.application.undo_execution import (
    ExecutionAlreadyUndoneError,
    UndoExecution,
    UndoExecutionCommand,
    UndoExecutionResult,
    UndoOutcome,
)
from safent_ads.execution.domain.execution_attempt import ExecutionAttempt, build_idempotency_key
from safent_ads.execution.domain.guardrails import GuardrailEvaluator
from safent_ads.execution.domain.identifiers import ExecutionId
from safent_ads.execution.infrastructure.sql_execution_queue import SqlExecutionQueue
from safent_ads.execution.infrastructure.sql_unit_of_work import SqlUnitOfWork
from safent_ads.execution.testing.fakes import FakeGuardrailSetRepository, FakeSpendLedger
from safent_ads.proposals.domain.authorization import (
    AuthorizationChannel,
    AuthorizationKind,
    sign_authorization,
)
from safent_ads.proposals.domain.cause import Cause, CauseKey, Evidence
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.identifiers import AuthorizationId, ProposalId
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
from tests.contracts.execution.conftest import NOW, digest
from tests.contracts.sql_fixtures import campaign_ref, seed_entity

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True)
class _ExecutedOutsideGrace:
    business_id: BusinessId
    proposal_id: ProposalId
    execution_id: ExecutionId


@dataclass(frozen=True, slots=True)
class _Scenario:
    """Mismo patron que `tests/integration/execution/conftest.py::
    CommittedScenario`: un motor propio, tantas sesiones/conexiones
    independientes como pida el caso -- necesario para que dos
    "transacciones-puerta" concurrentes de verdad se disputen el mismo
    candado de fila en Postgres."""

    engine: AsyncEngine
    seeded: _ExecutedOutsideGrace

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.engine.connect() as connection:
            session = AsyncSession(bind=connection, expire_on_commit=False)
            try:
                yield session
            finally:
                await session.rollback()
                await session.close()


def _build_proposal(business_id: BusinessId, entity_ref: EntityRef) -> Proposal:
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
        cause=Cause(text="CPL sobre objetivo en 7D", signal_id=None, rule_id=None),
        cause_key=CauseKey(entity_ref=entity_ref, rule_id="none", cause_type="cost_per_lead_high"),
        evidence=(Evidence(metric="cpl", actual=41.2, target=28.0, window_preset="7D"),),
        estimated_impact=Money.of("310"),
        priority=Priority(urgency=Urgency.RECOMMENDED),
        now=NOW,
        expires_at=NOW.replace(hour=23),
        expected_state_hash=digest(f"state-{entity_ref}"),
    )


async def _seed_executed_outside_grace(session: AsyncSession) -> _ExecutedOutsideGrace:
    """PENDING -> APPROVED -> SCHEDULED -> EXECUTING -> EXECUTED, un `save()`
    por salto (`proposals_guard_diff_hash`, 0008_proposals: un solo salto de
    estado por sentencia) -- igual que hace `ExecutionChokepoint` en
    produccion."""
    entity_ref = campaign_ref(f"undo-race-{uuid.uuid4().hex[:10]}")
    business_id = BusinessId(await seed_entity(session, entity_ref))
    proposal = _build_proposal(business_id, entity_ref)
    proposals = SqlProposalRepository(session)
    await proposals.save(proposal)

    proposal.approve(proposal.diff.diff_hash, NOW)
    await proposals.save(proposal)
    proposal.schedule_execution(0, NOW)
    await proposals.save(proposal)
    proposal.begin_execution(NOW)
    await proposals.save(proposal)
    proposal.record_execution(success=True, now=NOW)
    await proposals.save(proposal)

    authorization_id = AuthorizationId.new()
    authorization = sign_authorization(
        authorization_id=authorization_id,
        proposal_id=proposal.proposal_id,
        kind=AuthorizationKind.HUMAN_APPROVAL,
        proposal_classification=proposal.classification,
        diff_hash=proposal.diff.diff_hash,
        guardrail_verdict_hash=digest(f"verdict-{proposal.proposal_id}"),
        issued_by="owner-de-contrato",
        channel=AuthorizationChannel.PANEL,
        decided_at=NOW,
        expires_at=NOW.replace(hour=23),
        signer=FakeSignerPort(),
    )
    await SqlAuthorizationRepository(session).save(authorization)

    execution_id = ExecutionId.new()
    attempt = ExecutionAttempt(
        execution_id=execution_id,
        business_id=business_id,
        proposal_id=proposal.proposal_id,
        authorization_id=authorization_id,
        idempotency_key=build_idempotency_key(proposal.proposal_id, proposal.diff.diff_hash),
        platform_state_hash_before=proposal.expected_state_hash,
    )
    attempt.start_running()
    # Ventana de gracia ya cerrada (`undo_deadline` en el pasado): el camino
    # que toma `UndoExecution._undo_executed` es `_create_compensating_
    # proposal`, no `_restore_immediately` -- no hace falta un guardarraíl
    # ni un `spend_ledger` reales para este test.
    attempt.succeed("70", proposal.expected_state_hash, NOW - timedelta(minutes=1), NOW)
    await SqlExecutionQueue(session, FixedClock(NOW)).save(attempt)

    return _ExecutedOutsideGrace(
        business_id=business_id, proposal_id=proposal.proposal_id, execution_id=execution_id
    )


def _build_undo_execution(session: AsyncSession) -> UndoExecution:
    return UndoExecution(
        proposals=SqlProposalRepository(session),
        authorizations=SqlAuthorizationRepository(session),
        executions=SqlExecutionQueue(session, FixedClock(NOW)),
        uow=SqlUnitOfWork(session),
        guardrail_evaluator=GuardrailEvaluator(),
        guardrail_sets=FakeGuardrailSetRepository({}),
        spend_ledger=FakeSpendLedger({}),
        signer=FakeSignerPort(),
        clock=FixedClock(NOW),
    )


async def _undo(session: AsyncSession, proposal_id: ProposalId) -> UndoExecutionResult:
    use_case = _build_undo_execution(session)
    result = await use_case.execute(
        UndoExecutionCommand(proposal_id=proposal_id, initiated_by="qa-concurrency")
    )
    await session.commit()
    return result


async def test_two_concurrent_undos_on_the_same_execution_yield_exactly_one_winner(
    isolated_database_url: str,
) -> None:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as setup_connection:
            setup = AsyncSession(bind=setup_connection, expire_on_commit=False)
            seeded = await _seed_executed_outside_grace(setup)
            await setup.commit()
            await setup.close()
        scenario = _Scenario(engine=engine, seeded=seeded)

        async with scenario.session() as first, scenario.session() as second:
            outcomes = await asyncio.gather(
                _undo(first, scenario.seeded.proposal_id),
                _undo(second, scenario.seeded.proposal_id),
                return_exceptions=True,
            )

        successes = [o for o in outcomes if isinstance(o, UndoExecutionResult)]
        failures = [o for o in outcomes if isinstance(o, ExecutionAlreadyUndoneError)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert successes[0].outcome is UndoOutcome.COMPENSATING_PROPOSAL_CREATED

        async with scenario.session() as verify:
            compensating_count = (
                await verify.execute(
                    text(
                        "SELECT count(*) FROM proposals"
                        " WHERE business_id = :business_id AND cause LIKE 'Deshacer:%'"
                    ),
                    {"business_id": scenario.seeded.business_id.value},
                )
            ).scalar_one()
            assert compensating_count == 1

            marked_row = (
                await verify.execute(
                    text(
                        "SELECT undone_at, compensating_proposal_id FROM executions"
                        " WHERE id = :execution_id"
                    ),
                    {"execution_id": str(scenario.seeded.execution_id)},
                )
            ).one()
            assert marked_row.undone_at is not None
            assert str(marked_row.compensating_proposal_id) == str(
                successes[0].compensating_proposal_id
            )
    finally:
        await engine.dispose()
