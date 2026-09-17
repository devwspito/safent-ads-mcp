"""`RecordBridgeHealth` (spec 027 A-3): `has_recent_events_24h` se calcula
desde `revenue_events`, el estado del conector solo puede empeorarlo."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from safent_ads.crm.application.record_bridge_health import RecordBridgeHealth
from safent_ads.crm.testing.in_memory_repositories import (
    InMemoryCrmBridgeHealthRepository,
    InMemoryRevenueEventRepository,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()
_CONNECTOR_ID = "connector-crm"
_NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


class _StubRevenueEvents(InMemoryRevenueEventRepository):
    def __init__(self, last_event_at: datetime | None) -> None:
        super().__init__()
        self._last_event_at = last_event_at

    async def last_event_at(self, *, business_id: BusinessId, connector_id: str) -> datetime | None:
        del business_id, connector_id
        return self._last_event_at


async def test_ready_connector_with_recent_events_is_recorded_as_healthy() -> None:
    bridge_health = InMemoryCrmBridgeHealthRepository()
    use_case = RecordBridgeHealth(
        bridge_health=bridge_health,
        revenue_events=_StubRevenueEvents(_NOW - timedelta(hours=1)),
        clock=FixedClock(_NOW),
    )

    health = await use_case.execute(
        business_id=_BUSINESS_ID, connector_id=_CONNECTOR_ID, connector_state="listo", cause=None
    )

    assert health.has_recent_events_24h is True
    stored = await bridge_health.get_for_business(business_id=_BUSINESS_ID)
    assert stored is not None
    assert stored.has_recent_events_24h is True


async def test_degraded_connector_state_forces_unhealthy_even_with_fresh_events() -> None:
    use_case = RecordBridgeHealth(
        bridge_health=InMemoryCrmBridgeHealthRepository(),
        revenue_events=_StubRevenueEvents(_NOW - timedelta(minutes=5)),
        clock=FixedClock(_NOW),
    )

    health = await use_case.execute(
        business_id=_BUSINESS_ID,
        connector_id=_CONNECTOR_ID,
        connector_state="degradado",
        cause="credential_expired",
    )

    assert health.has_recent_events_24h is False
    assert health.cause == "credential_expired"
