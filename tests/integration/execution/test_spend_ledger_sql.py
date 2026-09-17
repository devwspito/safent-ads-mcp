"""`SqlSpendLedger` contra Postgres real: los topes se calculan sobre filas
de verdad (threat-model.md C-17) y las consultas que los calculan se
resuelven por el indice cubridor de `0009_executions`, no barriendo la
tabla."""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.execution.domain.execution_attempt import (
    ExecutionAttempt,
    build_idempotency_key,
)
from safent_ads.execution.domain.guardrails import (
    GuardrailChange,
    GuardrailEvaluator,
    GuardrailScope,
    GuardrailSet,
    ScopeKind,
)
from safent_ads.execution.domain.identifiers import ExecutionId
from safent_ads.execution.infrastructure.sql_execution_queue import SqlExecutionQueue
from safent_ads.execution.infrastructure.sql_spend_ledger import CAP_SUMS_SQL, SqlSpendLedger
from safent_ads.proposals.domain.authorization import AuthorizationKind
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.infrastructure.value_codec import encode_value
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import EntityRef
from tests.contracts.execution.conftest import (
    NOW,
    AuthorizedProposal,
    digest,
    seed_authorized_proposal,
)

pytestmark = pytest.mark.integration

# El dia local de la cuenta de los escenarios (`Europe/Madrid`) para
# `NOW = 2026-09-09 12:00 UTC`.
TODAY = date(2026, 9, 9)

_INSERT_LEDGER_ROW = """
    INSERT INTO spend_ledger (business_id, platform_account_id, entity_ref, ledger_date,
                              currency, kind, delta_minor, previous_value_minor,
                              new_value_minor, reported_spend_minor, execution_id)
    SELECT entity.business_id, entity.platform_account_id, entity.entity_ref, :ledger_date,
           'EUR', :kind, :delta_minor, :previous_minor, :new_minor, :reported_minor,
           :execution_id
      FROM ad_entities AS entity
     WHERE entity.entity_ref = :entity_ref
"""
_INSERT_APPLIED_CHANGE = _INSERT_LEDGER_ROW.replace(":kind", "'applied_change'").replace(
    ":reported_minor", "NULL"
)
_INSERT_PLATFORM_SPEND = (
    _INSERT_LEDGER_ROW.replace(":kind", "'platform_spend'")
    .replace(":previous_minor", "NULL")
    .replace(":new_minor", "NULL")
    .replace(":execution_id", "NULL")
)

# Ejecucion terminada a la que atribuir un cambio del ledger. El esquema
# exige valor aplicado y estado remoto confirmado para darla por buena.
_INSERT_EXECUTION = """
    INSERT INTO executions (proposal_id, authorization_id, business_id, entity_ref,
                            idempotency_key, previous_value, applied_value,
                            platform_state_hash_before, platform_state_hash_after, outcome,
                            finished_at)
    VALUES (:proposal_id, :authorization_id, :business_id, :entity_ref, :idempotency_key,
            CAST(:previous_value AS JSONB), CAST(:applied_value AS JSONB),
            :state_hash_before, :state_hash_after, 'SUCCEEDED', :finished_at)
    RETURNING id
"""


def ledger_for(
    session: AsyncSession, queue: SqlExecutionQueue | None = None
) -> SqlSpendLedger:
    """El ledger y la cola comparten instancia a proposito: el apunte se
    atribuye al intento que ESA cola reclamo."""
    clock = FixedClock(NOW)
    return SqlSpendLedger(session, clock, queue or SqlExecutionQueue(session, clock))


def entity_scope(entity_ref: EntityRef) -> GuardrailScope:
    return GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref))


async def given_platform_spend(
    session: AsyncSession, entity_ref: EntityRef, *, day: date, minor: int
) -> None:
    """Instantanea de gasto reportado por la plataforma (una por entidad y
    dia: el indice unico del esquema no admite dos)."""
    await session.execute(
        text(_INSERT_PLATFORM_SPEND),
        {
            "entity_ref": str(entity_ref),
            "ledger_date": day,
            "delta_minor": minor,
            "reported_minor": minor,
        },
    )


async def given_applied_change(
    session: AsyncSession, context: AuthorizedProposal, *, day: date, delta_minor: int
) -> None:
    """Cambio que otra ejecucion ya aplico. Cada apunte necesita la suya:
    `spend_ledger_applied_change_check` no admite un cambio aplicado sin
    ejecucion que lo respalde, que es lo que hace auditable el tope."""
    execution_id = await _given_finished_execution(session, context)
    await session.execute(
        text(_INSERT_APPLIED_CHANGE),
        {
            "entity_ref": str(context.entity_ref),
            "ledger_date": day,
            "delta_minor": delta_minor,
            "previous_minor": 0,
            "new_minor": delta_minor,
            "execution_id": execution_id,
        },
    )


async def _given_finished_execution(
    session: AsyncSession, context: AuthorizedProposal
) -> uuid.UUID:
    suffix = uuid.uuid4().hex[:12]
    execution_id = await session.scalar(
        text(_INSERT_EXECUTION),
        {
            "proposal_id": str(context.proposal_id),
            "authorization_id": str(context.authorization_id),
            "business_id": context.business_id.value,
            "entity_ref": str(context.entity_ref),
            "idempotency_key": f"exec-{context.proposal_id}-{suffix}",
            "previous_value": encode_value(Money.of("100")),
            "applied_value": encode_value(Money.of("70")),
            "state_hash_before": context.expected_state_hash,
            "state_hash_after": digest(f"after-{suffix}"),
            "finished_at": NOW,
        },
    )
    return uuid.UUID(str(execution_id))


async def claim_execution(
    session: AsyncSession, context: AuthorizedProposal
) -> SqlExecutionQueue:
    queue = SqlExecutionQueue(session, FixedClock(NOW))
    await queue.save(
        ExecutionAttempt(
            execution_id=ExecutionId.new(),
            business_id=context.business_id,
            proposal_id=context.proposal_id,
            authorization_id=context.authorization_id,
            idempotency_key=build_idempotency_key(context.proposal_id, context.diff_hash),
            previous_value=Money.of("100"),
            platform_state_hash_before=context.expected_state_hash,
            attempt_count=0,
        )
    )
    assert await queue.claim_next() is not None
    return queue


async def test_snapshot_adds_reported_spend_and_applied_changes(
    isolated_session: AsyncSession,
) -> None:
    context = await seed_authorized_proposal(isolated_session)
    await given_platform_spend(isolated_session, context.entity_ref, day=TODAY, minor=12_000)
    await given_platform_spend(
        isolated_session, context.entity_ref, day=TODAY - timedelta(days=1), minor=9_000
    )
    await given_applied_change(isolated_session, context, day=TODAY, delta_minor=2_500)

    snapshot = await ledger_for(isolated_session).snapshot(
        entity_scope(context.entity_ref), context.entity_ref
    )

    assert snapshot.platform_spend_today == Money.of("120")
    assert snapshot.platform_spend_month_to_date == Money.of("210")
    assert snapshot.applied_changes_today == Money.of("25")
    assert snapshot.changes_count_today_for_entity == 1


async def test_spend_of_another_account_does_not_count(
    isolated_session: AsyncSession,
) -> None:
    """Los topes son de una cuenta: el gasto de otra no consume su cupo
    (C-27, tambien en las sumas de dinero)."""
    mine = await seed_authorized_proposal(isolated_session)
    other = await seed_authorized_proposal(isolated_session)
    await given_platform_spend(isolated_session, other.entity_ref, day=TODAY, minor=50_000)

    snapshot = await ledger_for(isolated_session).snapshot(
        entity_scope(mine.entity_ref), mine.entity_ref
    )

    assert snapshot.platform_spend_today == Money.zero()


async def test_many_small_changes_hit_daily_cap(isolated_session: AsyncSession) -> None:
    """C-17, bypass por goteo: cuatro subidas de 30 EUR, cada una muy por
    debajo del salto maximo, suman 120 y el tope diario de 100 las corta. Sin
    el ledger, la quinta pasaria igual que la primera."""
    context = await seed_authorized_proposal(isolated_session)
    for _ in range(4):
        await given_applied_change(isolated_session, context, day=TODAY, delta_minor=3_000)

    snapshot = await ledger_for(isolated_session).snapshot(
        entity_scope(context.entity_ref), context.entity_ref
    )
    verdict = GuardrailEvaluator().evaluate(
        GuardrailChange(
            scope=entity_scope(context.entity_ref),
            entity_ref=context.entity_ref,
            authorization_kind=AuthorizationKind.HUMAN_APPROVAL,
            before=Money.of("100"),
            after=Money.of("110"),
        ),
        _guardrails(entity_scope(context.entity_ref)),
        snapshot,
    )

    assert snapshot.applied_changes_today == Money.of("120")
    assert not verdict.allowed
    assert "daily_cap_exceeded" in verdict.reasons


async def test_changes_count_is_per_entity_and_day(isolated_session: AsyncSession) -> None:
    context = await seed_authorized_proposal(isolated_session)
    await given_applied_change(isolated_session, context, day=TODAY, delta_minor=100)
    await given_applied_change(isolated_session, context, day=TODAY, delta_minor=100)
    await given_applied_change(
        isolated_session, context, day=TODAY - timedelta(days=1), delta_minor=100
    )

    snapshot = await ledger_for(isolated_session).snapshot(
        entity_scope(context.entity_ref), context.entity_ref
    )

    assert snapshot.changes_count_today_for_entity == 2


async def test_recording_twice_the_same_execution_keeps_one_row(
    isolated_session: AsyncSession,
) -> None:
    """`ix_spend_ledger_execution`: un apunte por ejecucion. Reintentar el
    registro no infla el tope."""
    context = await seed_authorized_proposal(isolated_session)
    queue = await claim_execution(isolated_session, context)
    ledger = ledger_for(isolated_session, queue)
    scope = entity_scope(context.entity_ref)

    await ledger.record_applied_change(scope, context.entity_ref, Money.of("-30"))
    await ledger.record_applied_change(scope, context.entity_ref, Money.of("-30"))

    snapshot = await ledger.snapshot(scope, context.entity_ref)
    assert snapshot.changes_count_today_for_entity == 1
    assert snapshot.applied_changes_today == Money.of("-30")


async def test_applied_change_keeps_the_value_before_and_after(
    isolated_session: AsyncSession,
) -> None:
    """`spend_ledger_applied_change_check` exige que el delta cuadre con el
    antes y el despues: el apunte no es un numero suelto, es un movimiento."""
    context = await seed_authorized_proposal(isolated_session)
    queue = await claim_execution(isolated_session, context)

    await ledger_for(isolated_session, queue).record_applied_change(
        entity_scope(context.entity_ref), context.entity_ref, Money.of("-30")
    )

    row = await isolated_session.execute(
        text(
            """
            SELECT previous_value_minor, new_value_minor, delta_minor, ledger_date
              FROM spend_ledger WHERE entity_ref = :entity_ref AND kind = 'applied_change'
            """
        ),
        {"entity_ref": str(context.entity_ref)},
    )
    movement = row.mappings().one()
    assert movement["previous_value_minor"] == 10_000
    assert movement["new_value_minor"] == 7_000
    assert movement["delta_minor"] == -3_000
    assert movement["ledger_date"] == TODAY


async def test_cap_sums_use_the_covering_index(isolated_session: AsyncSession) -> None:
    """C-17 tiene que seguir siendo barato: las sumas del tope se resuelven
    por `ix_spend_ledger_account_date ... INCLUDE (delta_minor)`, sin tocar
    la tabla. `enable_seqscan = off` no es trampa: en una tabla de test el
    planificador elegiria el barrido por tamano, y lo que se comprueba aqui
    es que el indice PUEDE resolver la consulta entera — si el INCLUDE o el
    orden de columnas cambiaran, no habria "Index Only Scan" que elegir."""
    context = await seed_authorized_proposal(isolated_session)
    for offset in range(30):
        await given_platform_spend(
            isolated_session,
            context.entity_ref,
            day=TODAY - timedelta(days=offset),
            minor=1_000 + offset,
        )
    account_id = await isolated_session.scalar(
        text("SELECT platform_account_id FROM ad_entities WHERE entity_ref = :ref"),
        {"ref": str(context.entity_ref)},
    )
    await isolated_session.execute(text("SET LOCAL enable_seqscan = off"))

    plan = await isolated_session.execute(
        text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {CAP_SUMS_SQL}"),
        {
            "platform_account_id": account_id,
            "today": TODAY,
            "month_start": TODAY.replace(day=1),
        },
    )
    rendered = json.dumps(plan.scalar_one())

    assert "ix_spend_ledger_account_date" in rendered
    assert "Index Only Scan" in rendered
    assert "Seq Scan" not in rendered


def _guardrails(scope: GuardrailScope) -> GuardrailSet:
    return GuardrailSet(
        scope=scope,
        daily_cap=Money.of("100"),
        monthly_cap=Money.of("2000"),
        floor=Money.of("10"),
        ceiling=Money.of("300"),
        max_step_pct=0.30,
        max_changes_per_entity_day=10,
    )
