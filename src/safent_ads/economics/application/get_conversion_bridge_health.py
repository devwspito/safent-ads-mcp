"""`GetConversionBridgeHealth` (tool-surface.md §2.5, contracts/mcp-tools.md
P2 `get_conversion_bridge_health`): "WhatsApp y llamada como conversion:
¿siguen llegando?". Un puente sano tuvo al menos un evento en las ultimas
`_HEALTHY_WITHIN_HOURS` horas -- sin eso, nodo 1 del diagnostico
('bridge_has_recent_events_24h')."""

from __future__ import annotations

from datetime import datetime, timedelta

from safent_ads.crm.application.ports import LeadAttributionRepository
from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.economics.application.dto import ConversionBridgeHealthView
from safent_ads.shared.ids import BusinessId

_BRIDGE_ACTIONS = (ConversionKind.WHATSAPP, ConversionKind.CALL)
_HEALTHY_WITHIN = timedelta(hours=24)


class GetConversionBridgeHealth:
    def __init__(self, lead_attributions: LeadAttributionRepository) -> None:
        self._lead_attributions = lead_attributions

    async def execute(
        self, *, business_id: BusinessId, as_of: datetime
    ) -> list[ConversionBridgeHealthView]:
        return [await self._row(business_id, action, as_of) for action in _BRIDGE_ACTIONS]

    async def _row(
        self, business_id: BusinessId, action: ConversionKind, as_of: datetime
    ) -> ConversionBridgeHealthView:
        last_event_at = await self._lead_attributions.last_event_at(
            business_id=business_id, conversion_kind=action
        )
        daily_count = await self._lead_attributions.count_by_kind_in_window(
            business_id=business_id,
            conversion_kind=action,
            window_start=as_of.date(),
            window_end=(as_of + timedelta(days=1)).date(),
        )
        healthy = last_event_at is not None and (as_of - last_event_at) <= _HEALTHY_WITHIN
        return ConversionBridgeHealthView(
            action=action.value,
            last_event_at=last_event_at,
            daily_count=daily_count,
            healthy=healthy,
        )
