"""`SqlRuleConditionPort` contra Postgres real: lo que solo se ve con el
catalogo, las senales y la frescura de verdad delante."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.execution.infrastructure.sql_freshness import SqlFreshnessPort
from safent_ads.execution.infrastructure.sql_rule_condition import SqlRuleConditionPort
from safent_ads.rules.infrastructure.sql_repositories import SqlRuleRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import EntityRef
from safent_ads.signals.infrastructure.sql_repositories import (
    SqlCreativeSignalRepository,
    SqlSignalRepository,
)
from tests.contracts.execution.conftest import (
    FIRING_RULE_CODE,
    NOW,
    calibrate_rule,
    seed_freshness,
    seed_sell_signal,
)
from tests.contracts.sql_fixtures import campaign_ref, seed_entity

pytestmark = pytest.mark.integration


def conditions_for(session: AsyncSession) -> SqlRuleConditionPort:
    return SqlRuleConditionPort(
        rules=SqlRuleRepository(session),
        signals=SqlSignalRepository(session, cycle_id=uuid.uuid4()),
        creative_signals=SqlCreativeSignalRepository(session, cycle_id=uuid.uuid4()),
        freshness=SqlFreshnessPort(session, FixedClock(NOW)),
    )


async def given_entity_with_signal(session: AsyncSession, *, lag_minutes: int) -> EntityRef:
    entity_ref = campaign_ref(f"c-{uuid.uuid4().hex[:10]}")
    await seed_entity(session, entity_ref)
    await seed_freshness(session, entity_ref, lag_minutes=lag_minutes)
    await seed_sell_signal(session, entity_ref)
    return entity_ref


async def test_an_uncalibrated_catalog_rule_does_not_fire(
    isolated_session: AsyncSession,
) -> None:
    """El catalogo nace sembrado y sin condicion (0007). Un `rule_id` que
    llega de un agente y apunta a una fila asi es un "no dispara", no una
    excepcion: el codigo viene de fuera y no se le da trato de invariante."""
    entity_ref = await given_entity_with_signal(isolated_session, lag_minutes=5)

    assert not await conditions_for(isolated_session).is_condition_live(
        FIRING_RULE_CODE, entity_ref
    )


async def test_an_unknown_rule_code_does_not_fire(isolated_session: AsyncSession) -> None:
    entity_ref = await given_entity_with_signal(isolated_session, lag_minutes=5)

    assert not await conditions_for(isolated_session).is_condition_live("Z99", entity_ref)


async def test_a_signal_older_than_the_last_ingestion_does_not_fire(
    isolated_session: AsyncSession,
) -> None:
    """Reevaluar de verdad: si llegaron metricas DESPUES de calcular la
    senal, ya no se sabe si la condicion sigue disparando. Se deniega hasta
    que el ciclo de senales vuelva a pronunciarse."""
    entity_ref = await given_entity_with_signal(isolated_session, lag_minutes=5)
    await calibrate_rule(isolated_session, enabled=True)
    await isolated_session.execute(
        text(
            """
            UPDATE data_freshness SET last_ingested_at = :later
             WHERE platform_account_id IN (SELECT platform_account_id FROM ad_entities
                                            WHERE entity_ref = :entity_ref)
            """
        ),
        {"later": NOW + timedelta(minutes=1), "entity_ref": str(entity_ref)},
    )

    assert not await conditions_for(isolated_session).is_condition_live(
        FIRING_RULE_CODE, entity_ref
    )
