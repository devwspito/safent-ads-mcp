"""`CrmLagObservationRepository`: adaptador real de `economics.application.
ports.LagObservationRepository` (T156) -- traduce `crm.LeadAttribution` via
`calendar_events` (`CalendarEventLookupPort`) en `LagObservation`
(`cohort_builder.build_lag_observations`), la capa anticorrupcion que el
puerto ya anunciaba en su docstring."""

from __future__ import annotations

from datetime import date

from safent_ads.crm.application.ports import LeadAttributionRepository
from safent_ads.economics.application.cohort_builder import build_lag_observations
from safent_ads.economics.application.ports import CalendarEventLookupPort
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.economics.domain.lag_curve import LagObservation
from safent_ads.shared.ids import BusinessId

__all__ = ["CrmLagObservationRepository"]


class CrmLagObservationRepository:
    def __init__(
        self, lead_attributions: LeadAttributionRepository, calendar_events: CalendarEventLookupPort
    ) -> None:
        self._lead_attributions = lead_attributions
        self._calendar_events = calendar_events

    async def fetch_observations(
        self, *, business_id: BusinessId, product_id: ProductId, platform: str, as_of: date
    ) -> list[LagObservation]:
        calendar_event_ids = await self._calendar_events.list_calendar_event_ids_for_product(
            business_id=business_id, product_id=product_id
        )
        if not calendar_event_ids:
            return []
        attributions = await self._lead_attributions.find_for_calendar_events(
            business_id=business_id, calendar_event_ids=calendar_event_ids
        )
        return build_lag_observations(attributions, platform=platform, as_of=as_of)
