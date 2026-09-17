"""0007_rules_guardrails: la semilla nace NOTIFY y deshabilitada, no pisa
lo que el propietario calibre, y lo autonomo solo puede defender."""

from __future__ import annotations

import asyncpg
import pytest

from tests.integration.migrations.conftest import (
    make_business,
    make_entity,
    make_platform_account,
)

pytestmark = pytest.mark.integration

_CATALOG_SIZE = 37


async def _catalog(pg: asyncpg.Connection) -> list[asyncpg.Record]:
    return await pg.fetch(
        "SELECT code, autonomy_level, is_enabled FROM rules WHERE scope = 'global' ORDER BY code"
    )


async def test_seed_is_idempotent(pg: asyncpg.Connection) -> None:
    before = await _catalog(pg)

    inserted = await pg.fetchval("SELECT seed_rule_catalog()")
    again = await pg.fetchval("SELECT seed_rule_catalog()")
    after = await _catalog(pg)

    assert inserted == 0, "la migracion ya sembro el catalogo"
    assert again == 0
    assert len(after) == _CATALOG_SIZE
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    assert {row["autonomy_level"] for row in after} == {"NOTIFY"}
    assert not any(row["is_enabled"] for row in after)


async def test_seed_never_clobbers_owner_calibration(pg: asyncpg.Connection) -> None:
    await pg.execute(
        """
        UPDATE rules
           SET action = 'SELL', data_window = '7D', cooldown_minutes = 1440,
               max_firings_per_day = 2, autonomy_level = 'AUTO', is_enabled = true,
               calibrated_at = now()
         WHERE code = 'M05' AND scope = 'global'
        """
    )

    await pg.fetchval("SELECT seed_rule_catalog()")

    row = await pg.fetchrow(
        "SELECT autonomy_level, is_enabled FROM rules WHERE code = 'M05' AND scope = 'global'"
    )
    assert row is not None
    assert row["autonomy_level"] == "AUTO"
    assert row["is_enabled"] is True

    await pg.execute(
        """
        UPDATE rules
           SET action = NULL, data_window = NULL, cooldown_minutes = NULL,
               max_firings_per_day = NULL, autonomy_level = 'NOTIFY', is_enabled = false,
               calibrated_at = NULL
         WHERE code = 'M05' AND scope = 'global'
        """
    )


async def test_autonomy_is_disabled_on_every_account(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)

    enabled = await pg.fetchval(
        "SELECT autonomy_enabled FROM platform_accounts WHERE id = $1", account_id
    )
    assert enabled is False


async def test_auto_rule_that_raises_spend_is_rejected(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)

    with pytest.raises(asyncpg.CheckViolationError, match="rules_auto_is_defensive_check"):
        await pg.execute(
            """
            INSERT INTO rules (code, scope, business_id, platform, action, data_window,
                               magnitude_pct, autonomy_level, cooldown_minutes,
                               max_firings_per_day, is_enabled)
            VALUES ('M02', 'business', $1, 'meta', 'BUY', '7D', 30, 'AUTO', 1440, 1, true)
            """,
            business_id,
        )


async def test_enabled_rule_must_be_complete(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)

    with pytest.raises(asyncpg.CheckViolationError, match="rules_enabled_is_complete_check"):
        await pg.execute(
            """
            INSERT INTO rules (code, scope, business_id, platform, autonomy_level, is_enabled)
            VALUES ('M05', 'business', $1, 'meta', 'NOTIFY', true)
            """,
            business_id,
        )


async def test_guardrail_floor_above_ceiling_is_rejected(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)

    with pytest.raises(asyncpg.CheckViolationError, match="guardrails_floor_ceiling_check"):
        await pg.execute(
            """
            INSERT INTO guardrails (scope, business_id, currency, budget_floor_minor,
                                    budget_ceiling_minor, max_step_pct,
                                    max_changes_per_entity_per_day)
            VALUES ('business', $1, 'EUR', 50000, 10000, 30, 2)
            """,
            business_id,
        )


async def test_one_guardrail_set_per_scope(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    statement = """
        INSERT INTO guardrails (scope, business_id, currency, budget_floor_minor,
                                budget_ceiling_minor, max_step_pct,
                                max_changes_per_entity_per_day)
        VALUES ('business', $1, 'EUR', 6000, 40000, 30, 2)
    """
    await pg.execute(statement, business_id)

    with pytest.raises(asyncpg.UniqueViolationError, match="guardrails_scope_unique"):
        await pg.execute(statement, business_id)


async def test_only_one_active_brake_per_scope(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    statement = """
        INSERT INTO emergency_brakes (scope_kind, business_id, mode, reason, engaged_by)
        VALUES ('business', $1, 'AUTONOMOUS', 'sangria de gasto', 'owner')
    """
    await pg.execute(statement, business_id)

    with pytest.raises(asyncpg.UniqueViolationError, match="ix_emergency_brakes_active"):
        await pg.execute(statement, business_id)

    await pg.execute(
        """
        UPDATE emergency_brakes SET released_at = now(), released_by = 'owner'
        WHERE business_id = $1
        """,
        business_id,
    )
    await pg.execute(statement, business_id)


async def test_rule_firing_is_idempotent_per_cycle(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)
    entity = await make_entity(pg, business_id, account_id)
    rule_id = await pg.fetchval(
        "SELECT id FROM rules WHERE code = 'M01' AND scope = 'global'"
    )
    cycle_id = await pg.fetchval("SELECT gen_random_uuid()")
    statement = """
        INSERT INTO rule_firings (rule_id, business_id, entity_ref, outcome, cycle_id)
        VALUES ($1, $2, $3, 'SUPPRESSED_COOLDOWN', $4)
    """
    await pg.execute(statement, rule_id, business_id, entity["entity_ref"], cycle_id)

    with pytest.raises(asyncpg.UniqueViolationError, match="rule_firings_cycle_unique"):
        await pg.execute(statement, rule_id, business_id, entity["entity_ref"], cycle_id)
