"""`SqlCrmBridgeHealthRepository` sobre `crm_bridge_health`
(0033_crm_bridge_health, spec 027 A-3): una fila viva por `(business_id,
connector_id)`."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.crm.domain.bridge_health import ConnectorBridgeState, CrmBridgeHealth
from safent_ads.shared.ids import BusinessId

__all__ = ["SqlCrmBridgeHealthRepository"]

_UPSERT = text("""
    INSERT INTO crm_bridge_health (
        business_id, connector_id, connector_state, last_event_at, has_recent_events_24h,
        cause, updated_at
    ) VALUES (
        :business_id, :connector_id, :connector_state, :last_event_at, :has_recent_events_24h,
        :cause, :updated_at
    )
    ON CONFLICT (business_id, connector_id) DO UPDATE SET
        connector_state = EXCLUDED.connector_state,
        last_event_at = EXCLUDED.last_event_at,
        has_recent_events_24h = EXCLUDED.has_recent_events_24h,
        cause = EXCLUDED.cause,
        updated_at = EXCLUDED.updated_at
""")

_GET_FOR_BUSINESS = text("""
    SELECT business_id, connector_id, connector_state, last_event_at, has_recent_events_24h,
           cause, updated_at
      FROM crm_bridge_health
     WHERE business_id = :business_id
     ORDER BY has_recent_events_24h ASC, updated_at DESC
     LIMIT 1
""")


class SqlCrmBridgeHealthRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, health: CrmBridgeHealth) -> None:
        await self._session.execute(
            _UPSERT,
            {
                "business_id": health.business_id.value,
                "connector_id": health.connector_id,
                "connector_state": health.connector_state.value,
                "last_event_at": health.last_event_at,
                "has_recent_events_24h": health.has_recent_events_24h,
                "cause": health.cause,
                "updated_at": health.updated_at,
            },
        )
        await self._session.flush()

    async def get_for_business(self, *, business_id: BusinessId) -> CrmBridgeHealth | None:
        """Cuando hay mas de un puente para el negocio, devuelve el peor
        (`has_recent_events_24h = false` primero): el freeze gate nunca
        debe leer un puente sano mientras otro esta roto (fail-closed)."""
        result = await self._session.execute(_GET_FOR_BUSINESS, {"business_id": business_id.value})
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return CrmBridgeHealth(
            business_id=BusinessId(row["business_id"]),
            connector_id=row["connector_id"],
            connector_state=ConnectorBridgeState(row["connector_state"]),
            last_event_at=row["last_event_at"],
            has_recent_events_24h=row["has_recent_events_24h"],
            cause=row["cause"],
            updated_at=row["updated_at"],
        )
