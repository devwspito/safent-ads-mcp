"""`SqlSignalReconciliationRepository` (tasks.md T116) contra Postgres real:
el adaptador de `metrics.application.ports.ActionableSignalPort` que
`signals` expone (`signals` puede depender de `metrics` en el grafo, al
reves no -- plan.md §4)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.shared.ids import BusinessId
from safent_ads.signals.domain.cause import Cause
from safent_ads.signals.domain.gate_verdict import GateName, GateVerdict
from safent_ads.signals.domain.signal import Signal, SignalKind
from safent_ads.signals.domain.value_objects import Evidence, MoneyAtStake, SignalStrength
from safent_ads.signals.domain.window_span import WindowSpan
from safent_ads.signals.infrastructure.sql_repositories import (
    SqlSignalReconciliationRepository,
    SqlSignalRepository,
)
from tests.conftest import rolled_back_session
from tests.contracts.sql_fixtures import campaign_ref, seed_entity

pytestmark = pytest.mark.integration

EMITTED_AT = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
CYCLE_ID = UUID("33333333-3333-4333-8333-333333333333")


@pytest.fixture
async def session(database_url: str) -> AsyncIterator[AsyncSession]:
    async with rolled_back_session(database_url) as open_session:
        yield open_session


def _buy_signal(*, rule_code: str = "M05") -> Signal:
    entity_ref = campaign_ref("reconciliacion")
    return Signal(
        entity_ref=entity_ref,
        kind=SignalKind.BUY,
        strength=SignalStrength(70),
        cause=Cause.ROAS_BELOW_TARGET_SUSTAINED,
        cause_sentence="Evidencia suficiente para escalar.",
        span=WindowSpan.D7,
        money_at_stake=MoneyAtStake(minor_units=31000, currency="EUR"),
        evidence=Evidence(
            metric="roas", actual=2.4, target=2.0, baseline=None, span=WindowSpan.D7
        ),
        gate_verdicts=(GateVerdict.ok(GateName.LEARNING),),
        emitted_at=EMITTED_AT,
        rule_code=rule_code,
    )


async def test_list_unresolved_returns_actionable_signals_up_to_cutoff(
    session: AsyncSession,
) -> None:
    signal = _buy_signal()
    business_id = await seed_entity(session, signal.entity_ref)
    await SqlSignalRepository(session, cycle_id=CYCLE_ID).save(signal)
    repository = SqlSignalReconciliationRepository(session)

    refs = await repository.list_unresolved(
        business_id=BusinessId(business_id), cutoff=EMITTED_AT + timedelta(hours=1)
    )

    assert len(refs) == 1
    ref = refs[0]
    assert ref.entity_ref == signal.entity_ref
    assert ref.rule_code == "M05"
    assert ref.window_end == EMITTED_AT.date()


async def test_list_unresolved_excludes_signals_after_cutoff(session: AsyncSession) -> None:
    signal = _buy_signal()
    business_id = await seed_entity(session, signal.entity_ref)
    await SqlSignalRepository(session, cycle_id=CYCLE_ID).save(signal)
    repository = SqlSignalReconciliationRepository(session)

    refs = await repository.list_unresolved(
        business_id=BusinessId(business_id), cutoff=EMITTED_AT - timedelta(hours=1)
    )

    assert refs == ()


async def test_mark_contradicted_removes_signal_from_unresolved(session: AsyncSession) -> None:
    signal = _buy_signal()
    business_id = await seed_entity(session, signal.entity_ref)
    await SqlSignalRepository(session, cycle_id=CYCLE_ID).save(signal)
    repository = SqlSignalReconciliationRepository(session)
    refs = await repository.list_unresolved(
        business_id=BusinessId(business_id), cutoff=EMITTED_AT + timedelta(hours=1)
    )

    await repository.mark_contradicted(
        signal_id=refs[0].signal_id, contradicted_at=EMITTED_AT + timedelta(minutes=5)
    )

    remaining = await repository.list_unresolved(
        business_id=BusinessId(business_id), cutoff=EMITTED_AT + timedelta(hours=1)
    )
    assert remaining == ()


async def test_mark_contradicted_is_idempotent(session: AsyncSession) -> None:
    signal = _buy_signal()
    business_id = await seed_entity(session, signal.entity_ref)
    await SqlSignalRepository(session, cycle_id=CYCLE_ID).save(signal)
    repository = SqlSignalReconciliationRepository(session)
    refs = await repository.list_unresolved(
        business_id=BusinessId(business_id), cutoff=EMITTED_AT + timedelta(hours=1)
    )
    signal_id = refs[0].signal_id

    await repository.mark_contradicted(
        signal_id=signal_id, contradicted_at=EMITTED_AT + timedelta(minutes=5)
    )
    await repository.mark_contradicted(
        signal_id=signal_id, contradicted_at=EMITTED_AT + timedelta(minutes=10)
    )

    remaining = await repository.list_unresolved(
        business_id=BusinessId(business_id), cutoff=EMITTED_AT + timedelta(hours=1)
    )
    assert remaining == ()


async def test_list_unresolved_excludes_hold_signals(session: AsyncSession) -> None:
    entity_ref = campaign_ref("reconciliacion-hold")
    business_id = await seed_entity(session, entity_ref)
    hold_signal = Signal(
        entity_ref=entity_ref,
        kind=SignalKind.HOLD,
        strength=SignalStrength(0),
        cause=Cause.ROAS_BELOW_TARGET_SUSTAINED,
        cause_sentence="Sin volumen minimo.",
        span=WindowSpan.D7,
        money_at_stake=MoneyAtStake(minor_units=0, currency="EUR"),
        evidence=Evidence(
            metric="roas", actual=0.0, target=None, baseline=None, span=WindowSpan.D7
        ),
        gate_verdicts=(GateVerdict.blocked(GateName.MIN_DATA, "sin volumen"),),
        emitted_at=EMITTED_AT,
        rule_code="M05",
    )
    await SqlSignalRepository(session, cycle_id=CYCLE_ID).save(hold_signal)
    repository = SqlSignalReconciliationRepository(session)

    refs = await repository.list_unresolved(
        business_id=BusinessId(business_id), cutoff=EMITTED_AT + timedelta(hours=1)
    )

    assert refs == ()
