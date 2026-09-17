"""`ListOfferingsWithEconomics` (T131, `GET /offerings`): las ofertas de un
negocio con su `offering_economics` si el dueño ya la relleno -- el panel
pinta "provisional" cuando `economics is None`."""

from __future__ import annotations

from safent_ads.economics.application.ports import OfferingEconomicsRepository, OfferingSummary
from safent_ads.shared.ids import BusinessId


class ListOfferingsWithEconomics:
    def __init__(self, *, offerings: OfferingEconomicsRepository) -> None:
        self._offerings = offerings

    async def execute(self, *, business_id: BusinessId) -> list[OfferingSummary]:
        return await self._offerings.list_offerings_with_economics(business_id=business_id)
