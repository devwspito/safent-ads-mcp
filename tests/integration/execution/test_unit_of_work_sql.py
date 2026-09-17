"""`SqlUnitOfWork` contra Postgres real: el reclamo de la cola y la
evaluacion del guardarraíl caben en UNA transaccion (threat-model.md C-15) y
deshacerla los deshace a los dos."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.execution.infrastructure.sql_unit_of_work import SqlUnitOfWork
from tests.contracts.execution.conftest import NOW
from tests.contracts.execution.test_execution_queue import claimable_attempt
from tests.integration.execution.conftest import CommittedScenario, committed_scenario
from tests.integration.execution.test_execution_queue_sql import queue_for

pytestmark = pytest.mark.integration

# Postgres bloquea de verdad en `pg_advisory_xact_lock`: un timeout corto
# basta para distinguir "esperando el candado de otra transaccion" de
# "siguio sin esperar nada" sin dejar el caso colgado si el fix se rompe.
_LOCK_WAIT_TIMEOUT = 2.0

_CLAIM_STATE = """
    SELECT outcome, started_at, attempt_count, error_code
      FROM executions WHERE proposal_id = :proposal_id
"""


class _GuardrailEvaluationFailed(RuntimeError):
    """Cualquier fallo evaluando las puertas: el ejemplo del caso."""


async def enqueue(scenario: CommittedScenario) -> None:
    async with scenario.session() as setup:
        await queue_for(setup).save(claimable_attempt(scenario.context))
        await setup.commit()


async def claim_state(session: AsyncSession, scenario: CommittedScenario) -> dict[str, object]:
    result = await session.execute(
        text(_CLAIM_STATE), {"proposal_id": str(scenario.context.proposal_id)}
    )
    return dict(result.mappings().one())


async def test_rollback_undoes_the_claim_and_the_verdict_together(
    isolated_database_url: str,
) -> None:
    """C-15: si algo estalla evaluando el guardarraíl, la fila vuelve a la
    cola tal cual estaba. Ni reclamada a medias ni bloqueada a medias."""
    async with committed_scenario(isolated_database_url) as scenario:
        await enqueue(scenario)

        async with scenario.session() as worker:
            queue = queue_for(worker)
            uow = SqlUnitOfWork(worker)
            attempt = await queue.claim_next()
            assert attempt is not None

            with pytest.raises(_GuardrailEvaluationFailed):
                async with uow:
                    attempt.block_by_guardrail(("daily_cap_exceeded",), NOW)
                    await queue.save(attempt)
                    raise _GuardrailEvaluationFailed

        async with scenario.session() as reader:
            state = await claim_state(reader, scenario)
            assert state["outcome"] == "CLAIMED"
            assert state["started_at"] is None
            assert state["attempt_count"] == 0
            assert state["error_code"] is None


async def test_the_unit_of_work_joins_the_claim_transaction(
    isolated_database_url: str,
) -> None:
    """El reclamo abre la transaccion; el `UnitOfWork` se une a ella. Si
    abriese una propia, el `SKIP LOCKED` y el veredicto viajarian en
    transacciones distintas y volveria el TOCTOU."""
    async with committed_scenario(isolated_database_url) as scenario:
        await enqueue(scenario)

        async with scenario.session() as worker:
            queue = queue_for(worker)
            assert not worker.in_transaction()

            assert await queue.claim_next() is not None
            assert worker.in_transaction()

            async with SqlUnitOfWork(worker):
                assert worker.get_nested_transaction() is None


async def test_committing_the_gates_makes_the_claim_firm(
    isolated_database_url: str,
) -> None:
    """Salir del bloque sin excepcion confirma: el desenlace del intento
    llega despues, con la escritura remota ya hecha, y para entonces el
    reclamo tiene que estar firme aunque el proceso muera."""
    async with committed_scenario(isolated_database_url) as scenario:
        await enqueue(scenario)

        async with scenario.session() as worker:
            queue = queue_for(worker)
            assert await queue.claim_next() is not None
            async with SqlUnitOfWork(worker):
                pass

        async with scenario.session() as reader:
            state = await claim_state(reader, scenario)
            assert state["started_at"] is not None
            assert state["attempt_count"] == 1

        async with scenario.session() as latecomer:
            assert await queue_for(latecomer).claim_next() is None


async def test_nested_blocks_do_not_commit_early(isolated_database_url: str) -> None:
    """`AuthorizeRuleAction` puede correr dentro del mismo `UnitOfWork` que
    el chokepoint: el bloque interior no confirma por su cuenta."""
    async with committed_scenario(isolated_database_url) as scenario:
        await enqueue(scenario)

        async with scenario.session() as worker:
            queue = queue_for(worker)
            attempt = await queue.claim_next()
            assert attempt is not None
            uow = SqlUnitOfWork(worker)

            async with uow:
                async with uow:
                    attempt.block_by_brake(NOW)
                    await queue.save(attempt)
                assert worker.in_transaction()
                await worker.rollback()

        async with scenario.session() as reader:
            state = await claim_state(reader, scenario)
            assert state["outcome"] == "CLAIMED"
            assert state["started_at"] is None


class TestLockAccount:
    """`SqlUnitOfWork.lock_account` (security review F2/F3, C-15/C-17 "no
    lock en el ambito del guardarraíl"): `pg_advisory_xact_lock` por
    `platform_account_id`, dentro de la transaccion-puerta."""

    async def test_a_second_transaction_on_the_same_account_waits_for_the_first(
        self, isolated_database_url: str
    ) -> None:
        async with committed_scenario(isolated_database_url) as scenario:
            async with scenario.session() as holder, scenario.session() as other:
                await SqlUnitOfWork(holder).lock_account(scenario.context.entity_ref)

                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(
                        SqlUnitOfWork(other).lock_account(scenario.context.entity_ref),
                        timeout=0.3,
                    )

    async def test_committing_the_first_releases_the_lock_for_the_second(
        self, isolated_database_url: str
    ) -> None:
        """No solo "se libera" -- lo que la primera escribio ANTES de
        confirmar queda visible para la segunda en cuanto arranca (la
        garantia que necesita C-17: "la segunda ve el apunte aplicado de la
        primera")."""
        async with committed_scenario(isolated_database_url) as scenario:
            async with scenario.session() as first:
                uow = SqlUnitOfWork(first)
                async with uow:
                    await uow.lock_account(scenario.context.entity_ref)
                    await queue_for(first).save(claimable_attempt(scenario.context))

            async with scenario.session() as second:
                await asyncio.wait_for(
                    SqlUnitOfWork(second).lock_account(scenario.context.entity_ref),
                    timeout=_LOCK_WAIT_TIMEOUT,
                )
                state = await claim_state(second, scenario)
                assert state["outcome"] == "CLAIMED"

    async def test_rollback_releases_the_lock_too(self, isolated_database_url: str) -> None:
        async with committed_scenario(isolated_database_url) as scenario:
            async with scenario.session() as first:
                await SqlUnitOfWork(first).lock_account(scenario.context.entity_ref)
                await first.rollback()

                async with scenario.session() as second:
                    await asyncio.wait_for(
                        SqlUnitOfWork(second).lock_account(scenario.context.entity_ref),
                        timeout=_LOCK_WAIT_TIMEOUT,
                    )

    async def test_locking_two_different_accounts_never_blocks(
        self, isolated_database_url: str
    ) -> None:
        """Cuentas distintas -> claves de lock distintas: nunca se pisan,
        sin depender de temporizacion mas alla de un timeout generoso."""
        async with (
            committed_scenario(isolated_database_url) as account_a,
            committed_scenario(isolated_database_url) as account_b,
        ):
            async with account_a.session() as holder:
                await SqlUnitOfWork(holder).lock_account(account_a.context.entity_ref)

                async with account_b.session() as other:
                    await asyncio.wait_for(
                        SqlUnitOfWork(other).lock_account(account_b.context.entity_ref),
                        timeout=_LOCK_WAIT_TIMEOUT,
                    )
