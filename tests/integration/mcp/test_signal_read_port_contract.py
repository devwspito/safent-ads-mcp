"""`SignalReadPort`: `FakeSignalReadPort` (`mcp/testing/fakes.py`) contra
`SqlSignalReadPort` (esta lane). `_derive_outcome` ya tiene su propio banco
de unidad puro (`test_sql_signal_read_port_outcome.py`) -- aqui solo se
prueba que el puerto compone bien encima de filas reales."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.contracts.sql_fixtures import account_external_id, campaign_ref, seed_entity

from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.mcp.infrastructure.sql_signal_read_port import SqlSignalReadPort
from safent_ads.mcp.testing.fakes import BUSINESS_A, FakeSignalReadPort
from safent_ads.rules.domain.guardrail import GuardrailPolicy
from safent_ads.rules.infrastructure.sql_repositories import SqlGuardrailRepository
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)

_INSERT_SIGNAL = text("""
    INSERT INTO signals (business_id, entity_ref, kind, strength, cause, cause_code,
                         rule_code, gate_verdicts, evidence, data_window, window_start,
                         window_end, money_at_stake_minor, money_at_stake_currency,
                         cycle_id, emitted_at)
    VALUES (:business_id, :entity_ref, :kind, 78, 'CPL 41 vs 28', 'CPL_OVER_TARGET', 'M05',
            CAST(:gate_verdicts AS jsonb), CAST(:evidence AS jsonb),
            '7D', :window_start, :window_end, 31000, 'EUR', :cycle_id, :emitted_at)
    RETURNING id
""")

# Los dos puntos de un literal JSON en linea chocan con el patron `:nombre`
# que `sqlalchemy.text()` usa para detectar parametros con nombre (incluso
# `:41` cuenta, `\w` incluye digitos) -- por eso `evidence` viaja como
# parametro serializado, igual que `signals.infrastructure.sql_repositories.
# _encode_evidence`, nunca como texto JSON pegado en la sentencia.
_EVIDENCE_JSON = '{"metric":"cpl","actual":41,"target":28,"baseline":30,"span":"7D"}'

_INSERT_ANOMALY = text("""
    INSERT INTO anomalies (business_id, entity_ref, method, metric, score, severity,
                           interval_start, evidence, cycle_id, detected_at)
    VALUES (:business_id, :entity_ref, 'ewma', 'spend_minor', 3.4, 'PAGE', :detected_at,
            '{}'::jsonb, :cycle_id, :detected_at)
""")


@dataclass(slots=True)
class SignalFixture:
    port: object
    business_id: str
    signal_id: str
    entity_ref: str


async def _seed_sql_signal(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, str]:
    entity_ref = campaign_ref(f"sig{uuid.uuid4().hex[:8]}", platform_value="google")
    async with factory() as session:
        business_id = await seed_entity(session, entity_ref)
        signal_id = (
            await session.execute(
                _INSERT_SIGNAL,
                {
                    "business_id": business_id,
                    "entity_ref": str(entity_ref),
                    "kind": "SELL",
                    "gate_verdicts": "[]",
                    "evidence": _EVIDENCE_JSON,
                    "window_start": _NOW.date() - timedelta(days=6),
                    "window_end": _NOW.date(),
                    "cycle_id": uuid.uuid4(),
                    "emitted_at": _NOW - timedelta(days=2),
                },
            )
        ).scalar_one()
        await session.execute(
            _INSERT_ANOMALY,
            {
                "business_id": business_id,
                "entity_ref": str(entity_ref),
                "cycle_id": uuid.uuid4(),
                "detected_at": _NOW - timedelta(hours=1),
            },
        )
        await SqlGuardrailRepository(session).save_for_account(
            account_ref=f"google:{account_external_id(entity_ref)}",
            policy=GuardrailPolicy(
                daily_cap_minor=10_000,
                monthly_cap_minor=200_000,
                floor_minor=0,
                ceiling_minor=200_000,
                max_step_pct=20.0,
                max_changes_per_day=5,
            ),
            currency="EUR",
        )
        await session.commit()
    return business_id, signal_id, str(entity_ref)


@pytest.fixture(
    params=[
        pytest.param("fake", id="fake"),
        pytest.param("sql", id="sql", marks=pytest.mark.integration),
    ]
)
async def signal(request: pytest.FixtureRequest) -> AsyncIterator[SignalFixture]:
    if request.param == "fake":
        fake = FakeSignalReadPort()
        page = await fake.list_signals(
            BUSINESS_A, kind=None, min_strength=None, since=None, limit=10, cursor=None
        )
        item = page.items[0]
        yield SignalFixture(fake, BUSINESS_A, item.signal_id, item.entity_ref)
        return

    factory: async_sessionmaker[AsyncSession] = request.getfixturevalue("mcp_session_factory")
    business_id, signal_id, entity_ref = await _seed_sql_signal(factory)
    port = SqlSignalReadPort(factory, clock=FixedClock(_NOW))
    yield SignalFixture(port, str(business_id), str(signal_id), entity_ref)


async def test_list_signals_includes_the_seeded_signal(signal: SignalFixture) -> None:
    page = await signal.port.list_signals(
        signal.business_id, kind=None, min_strength=None, since=None, limit=10, cursor=None
    )

    assert any(item.signal_id == signal.signal_id for item in page.items)


async def test_get_signal_echoes_the_requested_id(signal: SignalFixture) -> None:
    detail = await signal.port.get_signal(signal.business_id, signal.signal_id)

    assert detail.summary.signal_id == signal.signal_id


async def test_explain_signal_fills_in_a_narrative(signal: SignalFixture) -> None:
    detail = await signal.port.explain_signal(signal.business_id, signal.signal_id)

    assert detail.narrative


async def test_list_anomalies_returns_at_least_one(signal: SignalFixture) -> None:
    anomalies = await signal.port.list_anomalies(
        signal.business_id, since=_NOW - timedelta(days=7)
    )

    assert len(anomalies) >= 1


async def test_get_pacing_returns_a_pacing_snapshot(signal: SignalFixture) -> None:
    pacing = await signal.port.get_pacing(signal.business_id, signal.entity_ref)

    assert pacing.entity_ref == signal.entity_ref


@pytest.mark.integration
async def test_sql_get_signal_rejects_signal_from_another_business(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _owner_business_id, signal_id, _entity_ref = await _seed_sql_signal(mcp_session_factory)
    port = SqlSignalReadPort(mcp_session_factory, clock=FixedClock(_NOW))

    with pytest.raises(EntityNotFoundError):
        await port.get_signal(str(uuid.uuid4()), str(signal_id))


@pytest.mark.integration
async def test_sql_recent_signal_outcome_is_in_progress(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    business_id, signal_id, _entity_ref = await _seed_sql_signal(mcp_session_factory)
    port = SqlSignalReadPort(mcp_session_factory, clock=FixedClock(_NOW))

    detail = await port.get_signal(str(business_id), str(signal_id))

    assert detail.summary.outcome.status.value == "in_progress"
    assert detail.summary.outcome.days_remaining == 12


@pytest.mark.integration
async def test_sql_get_pacing_rejects_entity_from_another_business(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _owner_business_id, _signal_id, entity_ref = await _seed_sql_signal(mcp_session_factory)
    port = SqlSignalReadPort(mcp_session_factory, clock=FixedClock(_NOW))

    with pytest.raises(EntityNotFoundError):
        await port.get_pacing(str(uuid.uuid4()), entity_ref)
