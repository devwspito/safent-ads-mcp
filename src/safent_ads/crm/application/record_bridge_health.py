"""`RecordBridgeHealth` (spec 027 A-3, contracts/crm-link.md §2 `PUT
/crm/bridge-health`): el compañero CALCULA `has_recent_events_24h` desde su
propio ultimo `RevenueEvent` -- el estado de conector que reporta el
runtime solo puede empeorar esa lectura, nunca mejorarla."""

from __future__ import annotations

from safent_ads.crm.application.customer_ports import (
    CrmBridgeHealthRepository,
    RevenueEventRepository,
)
from safent_ads.crm.domain.bridge_health import ConnectorBridgeState, CrmBridgeHealth
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId


class RecordBridgeHealth:
    def __init__(
        self,
        *,
        bridge_health: CrmBridgeHealthRepository,
        revenue_events: RevenueEventRepository,
        clock: Clock,
    ) -> None:
        self._bridge_health = bridge_health
        self._revenue_events = revenue_events
        self._clock = clock

    async def execute(
        self,
        *,
        business_id: BusinessId,
        connector_id: str,
        connector_state: str,
        cause: str | None,
    ) -> CrmBridgeHealth:
        last_event_at = await self._revenue_events.last_event_at(
            business_id=business_id, connector_id=connector_id
        )
        health = CrmBridgeHealth.evaluate(
            business_id=business_id,
            connector_id=connector_id,
            connector_state=ConnectorBridgeState(connector_state),
            last_event_at=last_event_at,
            as_of=self._clock.now(),
            cause=cause,
        )
        await self._bridge_health.upsert(health)
        return health
