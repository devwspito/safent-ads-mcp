"""0006_signals: el indice parcial `kind <> 'HOLD'` es el que sirve al
ticker, y una HOLD sin motivo de puerta no entra."""

from __future__ import annotations

import asyncpg
import pytest

from tests.integration.migrations.conftest import (
    make_business,
    make_entity,
    make_platform_account,
)

pytestmark = pytest.mark.integration

_SEED_SIGNALS = """
    INSERT INTO signals (business_id, entity_ref, kind, strength, cause, cause_code, rule_code,
                         gate_reason, data_window, window_start, window_end,
                         money_at_stake_minor, money_at_stake_currency, cycle_id, emitted_at)
    SELECT $1, $2,
           CASE WHEN n % 12 = 0 THEN 'SELL' ELSE 'HOLD' END,
           n % 101,
           'causa de prueba',
           'roas_below_target_sustained', 'M05',
           CASE WHEN n % 12 = 0 THEN NULL ELSE 'puerta de aprendizaje sin superar' END,
           '7D', current_date - 7, current_date,
           n % 977, 'EUR', gen_random_uuid(),
           now() - make_interval(mins => n)
      FROM generate_series(1, 4000) AS n
"""

# Consulta del ticker: accionables de un negocio por dinero en juego.
_EXPLAIN_TICKER = """
    EXPLAIN
    SELECT id, money_at_stake_minor
      FROM signals
     WHERE business_id = $1 AND kind <> 'HOLD'
     ORDER BY money_at_stake_minor DESC
     LIMIT 20
"""


async def test_partial_index_used(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)
    entity = await make_entity(pg, business_id, account_id)
    await pg.execute(_SEED_SIGNALS, business_id, entity["entity_ref"])
    await pg.execute("ANALYZE signals")

    plan = "\n".join(row[0] for row in await pg.fetch(_EXPLAIN_TICKER, business_id))

    assert "ix_signals_actionable" in plan, plan


async def test_hold_without_gate_reason_is_rejected(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)
    entity = await make_entity(pg, business_id, account_id)

    with pytest.raises(asyncpg.CheckViolationError, match="signals_hold_needs_reason"):
        await pg.execute(
            """
            INSERT INTO signals (business_id, entity_ref, kind, strength, cause, cause_code,
                                 rule_code, data_window, window_start, window_end,
                                 money_at_stake_minor, money_at_stake_currency, cycle_id)
            VALUES ($1, $2, 'HOLD', 10, 'sin motivo', 'gate_blocked', 'M23', '7D',
                    current_date - 7, current_date, 0, 'EUR', gen_random_uuid())
            """,
            business_id,
            entity["entity_ref"],
        )


async def test_strength_out_of_range_is_rejected(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)
    entity = await make_entity(pg, business_id, account_id)

    with pytest.raises(asyncpg.CheckViolationError, match="strength"):
        await pg.execute(
            """
            INSERT INTO signals (business_id, entity_ref, kind, strength, cause, cause_code,
                                 rule_code, data_window, window_start, window_end,
                                 money_at_stake_minor, money_at_stake_currency, cycle_id)
            VALUES ($1, $2, 'SELL', 140, 'demasiada fuerza', 'cpa_above_target', 'M06', '7D',
                    current_date - 7, current_date, 10, 'EUR', gen_random_uuid())
            """,
            business_id,
            entity["entity_ref"],
        )


async def test_signal_cannot_point_to_another_business_entity(pg: asyncpg.Connection) -> None:
    owner_id = await make_business(pg)
    intruder_id = await make_business(pg)
    account_id = await make_platform_account(pg, owner_id)
    entity = await make_entity(pg, owner_id, account_id)

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await pg.execute(
            """
            INSERT INTO signals (business_id, entity_ref, kind, strength, cause, cause_code,
                                 rule_code, data_window, window_start, window_end,
                                 money_at_stake_minor, money_at_stake_currency, cycle_id)
            VALUES ($1, $2, 'SELL', 50, 'fuga entre negocios', 'cpa_above_target', 'M06', '7D',
                    current_date - 7, current_date, 10, 'EUR', gen_random_uuid())
            """,
            intruder_id,
            entity["entity_ref"],
        )


async def test_anomaly_is_not_duplicated_per_interval(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)
    entity = await make_entity(pg, business_id, account_id)
    statement = """
        INSERT INTO anomalies (business_id, entity_ref, method, metric, score, severity,
                               interval_start, cycle_id)
        VALUES ($1, $2, 'weekday_z', 'spend', 3.2, 'WARN',
                '2026-03-14 10:00+00', gen_random_uuid())
    """
    await pg.execute(statement, business_id, entity["entity_ref"])

    with pytest.raises(asyncpg.UniqueViolationError, match="anomalies_natural_unique"):
        await pg.execute(statement, business_id, entity["entity_ref"])
