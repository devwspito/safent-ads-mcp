"""`LiveOpportunityStep`: adaptador real de `OpportunityStepPort` (T113).
Cablea `GenerateOpportunities` (opportunities/application) sobre sus
adaptadores SQL reales -- mismo patron que `LiveEconomicsStep` (una sesion
propia por negocio, commit al final)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.opportunities.application.generate_opportunities import GenerateOpportunities
from safent_ads.opportunities.infrastructure.sql_repositories import (
    SqlCalendarEventGapPort,
    SqlCampaignProposalPort,
    SqlDailyCandidateBudgetPort,
    SqlOfferingContributionPort,
)
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId


class LiveOpportunityStep:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def run(self, business_id: BusinessId, cycle_id: str, now: datetime) -> None:
        del cycle_id, now
        async with self._session_factory() as session:
            await self._build_use_case(session).execute(business_id=business_id)
            await session.commit()

    def _build_use_case(self, session: AsyncSession) -> GenerateOpportunities:
        return GenerateOpportunities(
            calendar_event_gaps=SqlCalendarEventGapPort(session),
            offering_contribution=SqlOfferingContributionPort(session, self._clock),
            daily_budget=SqlDailyCandidateBudgetPort(session),
            campaign_proposals=SqlCampaignProposalPort(session, clock=self._clock),
            clock=self._clock,
        )
