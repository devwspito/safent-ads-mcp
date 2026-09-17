"""Banco de contrato de `SignalRepository`, `CreativeSignalRepository` y
`AnomalyRepository`.

`AnomalyRepository` solo declara `save`; para comprobar que lo guardado se
recupera, la fixture expone como se observa cada implementacion (la lista
del doble, la consulta del repositorio SQL). El caso de prueba es el mismo."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from safent_ads.shared.ids import EntityRef
from safent_ads.signals.application.ports import (
    AnomalyRepository,
    CreativeSignalRepository,
    SignalRepository,
)
from safent_ads.signals.domain.anomaly import Anomaly
from safent_ads.signals.domain.cause import Cause
from safent_ads.signals.domain.gate_verdict import GateName, GateVerdict
from safent_ads.signals.domain.signal import CreativeSignal, CreativeSignalKind, Signal, SignalKind
from safent_ads.signals.domain.value_objects import Evidence, MoneyAtStake, SignalStrength
from safent_ads.signals.domain.window_span import WindowSpan
from safent_ads.signals.infrastructure.sql_repositories import (
    SqlAnomalyRepository,
    SqlCreativeSignalRepository,
    SqlSignalRepository,
)
from safent_ads.signals.testing.in_memory_anomaly_repository import InMemoryAnomalyRepository
from safent_ads.signals.testing.in_memory_signal_repository import (
    InMemoryCreativeSignalRepository,
    InMemorySignalRepository,
)
from tests.conftest import rolled_back_session
from tests.contracts.sql_fixtures import seed_entity

EMITTED_AT = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
CYCLE_ID = UUID("22222222-2222-4222-8222-222222222222")


@dataclass(slots=True)
class SignalsFixture:
    signals: SignalRepository
    creative_signals: CreativeSignalRepository
    anomalies: AnomalyRepository
    observe_anomalies: Callable[[EntityRef], Any]
    session: Any

    async def given_entity(self, entity_ref: EntityRef) -> None:
        if self.session is not None:
            await seed_entity(self.session, entity_ref)


@pytest.fixture(
    params=[
        pytest.param("in_memory", id="in_memory"),
        pytest.param("sql", id="sql", marks=pytest.mark.integration),
    ]
)
async def signals(request: pytest.FixtureRequest) -> AsyncIterator[SignalsFixture]:
    if request.param == "in_memory":
        anomalies = InMemoryAnomalyRepository()

        async def observe(entity_ref: EntityRef) -> Sequence[Anomaly]:
            return [item for item in anomalies.saved if item.entity_ref == entity_ref]

        yield SignalsFixture(
            signals=InMemorySignalRepository(),
            creative_signals=InMemoryCreativeSignalRepository(),
            anomalies=anomalies,
            observe_anomalies=observe,
            session=None,
        )
        return
    database_url: str = request.getfixturevalue("database_url")
    async with rolled_back_session(database_url) as session:
        sql_anomalies = SqlAnomalyRepository(session, cycle_id=CYCLE_ID)

        async def observe_sql(entity_ref: EntityRef) -> Sequence[Anomaly]:
            return await sql_anomalies.list_for_entity(entity_ref=entity_ref)

        yield SignalsFixture(
            signals=SqlSignalRepository(session, cycle_id=CYCLE_ID),
            creative_signals=SqlCreativeSignalRepository(session, cycle_id=CYCLE_ID),
            anomalies=sql_anomalies,
            observe_anomalies=observe_sql,
            session=session,
        )


def build_signal(
    entity_ref: EntityRef,
    *,
    kind: SignalKind = SignalKind.SELL,
    strength: int = 78,
    emitted_at: datetime = EMITTED_AT,
    gate_verdicts: tuple[GateVerdict, ...] = (),
) -> Signal:
    return Signal(
        entity_ref=entity_ref,
        kind=kind,
        strength=SignalStrength(strength),
        cause=Cause.ROAS_BELOW_TARGET_SUSTAINED,
        cause_sentence="ROAS 1.10 por debajo del objetivo 2.00 en 3 y 7 dias.",
        span=WindowSpan.D7,
        money_at_stake=MoneyAtStake(minor_units=31000, currency="EUR"),
        evidence=Evidence(
            metric="roas", actual=1.1, target=2.0, baseline=None, span=WindowSpan.D7
        ),
        gate_verdicts=gate_verdicts or (GateVerdict.ok(GateName.LEARNING),),
        emitted_at=emitted_at,
        rule_code="M05",
    )


def build_creative_signal(
    entity_ref: EntityRef, *, kind: CreativeSignalKind = CreativeSignalKind.FATIGUE
) -> CreativeSignal:
    return CreativeSignal(
        entity_ref=entity_ref,
        kind=kind,
        strength=SignalStrength(64),
        cause=Cause.CTR_DECLINE_VS_BASELINE,
        cause_sentence="CTR 0.60% un 25% por debajo de su linea base de 14 dias.",
        span=WindowSpan.D14,
        money_at_stake=MoneyAtStake(minor_units=12000, currency="EUR"),
        evidence=Evidence(
            metric="ctr", actual=0.006, target=None, baseline=0.008, span=WindowSpan.D14
        ),
        gate_verdicts=(GateVerdict.blocked(GateName.MIN_DATA, "menos de 1.000 impresiones"),),
        emitted_at=EMITTED_AT,
        rule_code="M16",
    )
