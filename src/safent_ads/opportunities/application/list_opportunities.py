"""`ListOpportunities` (tasks.md T114): lectura de las propuestas
`CREATE_CAMPAIGN` todavia abiertas de un negocio, ya ordenadas por
`expected_contribution_delta` (T113). Sin logica propia -- el puerto ya
entrega la proyeccion decodificada; mismo patron trivial que
`optimization.application.get_calibration_report.GetCalibrationReport`."""

from __future__ import annotations

from safent_ads.opportunities.application.ports import OpenOpportunityPort, OpenOpportunityView
from safent_ads.shared.ids import BusinessId


class ListOpportunities:
    def __init__(self, *, opportunities: OpenOpportunityPort) -> None:
        self._opportunities = opportunities

    async def execute(self, *, business_id: BusinessId) -> tuple[OpenOpportunityView, ...]:
        return await self._opportunities.list_open(business_id=business_id)
