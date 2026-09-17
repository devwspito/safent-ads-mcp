"""`SqlMeasurementFreezeGate` lee `crm_bridge_health` real (spec 027 T017,
A-3): sin puente configurado nunca congela; con el puente roto, congela; al
recuperarse, se descongela en el ciclo siguiente."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from safent_ads.accounts.domain.platform_account import PlatformAccount
from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.accounts.infrastructure.sql_repositories import SqlAccountRepository
from safent_ads.crm.domain.bridge_health import ConnectorBridgeState, CrmBridgeHealth
from safent_ads.crm.infrastructure.sql_crm_bridge_health_repository import (
    SqlCrmBridgeHealthRepository,
)
from safent_ads.orchestration.infrastructure.measurement_freeze_gate import (
    SqlMeasurementFreezeGate,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityRef
from tests.contracts.sql_fixtures import account_external_id, campaign_ref, seed_entity

pytestmark = pytest.mark.integration

_AS_OF = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
_CONNECTOR_ID = "connector-crm"


@pytest.fixture
async def seeded(
    isolated_database_url: str,
) -> AsyncIterator[tuple[AsyncSession, BusinessId, PlatformAccount]]:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    entity_ref = campaign_ref(f"freeze-bridge-{id(engine)}", platform_value="google")
    session = AsyncSession(engine, expire_on_commit=False)
    business_id = BusinessId(await seed_entity(session, entity_ref))
    await session.commit()
    account = await SqlAccountRepository(session).get_by_ref(_account_ref(entity_ref))
    assert account is not None
    try:
        yield session, business_id, account
    finally:
        await session.close()
        await engine.dispose()


def _account_ref(entity_ref: EntityRef) -> AccountRef:
    return AccountRef(entity_ref.platform, account_external_id(entity_ref))


async def test_unconfigured_bridge_never_freezes(
    seeded: tuple[AsyncSession, BusinessId, PlatformAccount],
) -> None:
    session, business_id, account = seeded
    gate = SqlMeasurementFreezeGate(session, FixedClock(_AS_OF))

    frozen = await gate.is_frozen(business_id, account)

    assert frozen is False


async def test_broken_bridge_freezes_buy(
    seeded: tuple[AsyncSession, BusinessId, PlatformAccount],
) -> None:
    session, business_id, account = seeded
    await SqlCrmBridgeHealthRepository(session).upsert(
        CrmBridgeHealth.evaluate(
            business_id=business_id,
            connector_id=_CONNECTOR_ID,
            connector_state=ConnectorBridgeState.DEGRADED,
            last_event_at=_AS_OF - timedelta(hours=48),
            as_of=_AS_OF,
            cause="credential_expired",
        )
    )
    await session.commit()
    gate = SqlMeasurementFreezeGate(session, FixedClock(_AS_OF))

    frozen = await gate.is_frozen(business_id, account)

    assert frozen is True


async def test_recovered_bridge_unfreezes_on_the_next_cycle(
    seeded: tuple[AsyncSession, BusinessId, PlatformAccount],
) -> None:
    session, business_id, account = seeded
    bridge_health = SqlCrmBridgeHealthRepository(session)
    await bridge_health.upsert(
        CrmBridgeHealth.evaluate(
            business_id=business_id,
            connector_id=_CONNECTOR_ID,
            connector_state=ConnectorBridgeState.DEGRADED,
            last_event_at=_AS_OF - timedelta(hours=48),
            as_of=_AS_OF,
            cause="credential_expired",
        )
    )
    await session.commit()
    broken_gate = SqlMeasurementFreezeGate(session, FixedClock(_AS_OF))
    assert await broken_gate.is_frozen(business_id, account) is True

    recovered_at = _AS_OF + timedelta(hours=1)
    await bridge_health.upsert(
        CrmBridgeHealth.evaluate(
            business_id=business_id,
            connector_id=_CONNECTOR_ID,
            connector_state=ConnectorBridgeState.READY,
            last_event_at=recovered_at,
            as_of=recovered_at,
            cause=None,
        )
    )
    await session.commit()
    recovered_gate = SqlMeasurementFreezeGate(session, FixedClock(recovered_at))

    assert await recovered_gate.is_frozen(business_id, account) is False
