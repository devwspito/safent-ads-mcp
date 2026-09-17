"""Cablea los puertos de `opportunities` a Postgres real: una sesion por
llamada via `session_factory`, mismo patron que
`optimization.infrastructure.request_scoped_repositories` -- cada adaptador
de `opportunities/infrastructure/sql_repositories.py` esta atado a UNA
`AsyncSession`, y `composition/app.py` construye estos envoltorios UNA vez
al arrancar la app."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.opportunities.application.ports import (
    CampaignProposalOutcome,
    OpenOpportunityView,
)
from safent_ads.opportunities.domain.campaign_brief import CampaignBrief
from safent_ads.opportunities.infrastructure.sql_repositories import (
    SqlAccountDailyCapPort,
    SqlActiveAccountLookupPort,
    SqlCampaignProposalPort,
    SqlOfferingExistsPort,
    SqlOpenOpportunityPort,
)
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId, EntityRef, PlatformCode

__all__ = [
    "RequestScopedAccountDailyCap",
    "RequestScopedActiveAccountLookup",
    "RequestScopedCampaignProposals",
    "RequestScopedOfferingExists",
    "RequestScopedOpenOpportunities",
]


class RequestScopedOfferingExists:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def exists(self, *, business_id: BusinessId, offering_id: str) -> bool:
        async with self._session_factory() as session:
            return await SqlOfferingExistsPort(session).exists(
                business_id=business_id, offering_id=offering_id
            )


class RequestScopedActiveAccountLookup:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def find_active_account(
        self,
        *,
        business_id: BusinessId,
        platform: PlatformCode,
        account_ref: EntityRef | None = None,
    ) -> EntityRef | None:
        async with self._session_factory() as session:
            return await SqlActiveAccountLookupPort(session).find_active_account(
                business_id=business_id, platform=platform, account_ref=account_ref
            )


class RequestScopedAccountDailyCap:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_daily_cap(self, *, account_ref: EntityRef) -> Money | None:
        async with self._session_factory() as session:
            return await SqlAccountDailyCapPort(session).get_daily_cap(account_ref=account_ref)


class RequestScopedOpenOpportunities:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_open(self, *, business_id: BusinessId) -> tuple[OpenOpportunityView, ...]:
        async with self._session_factory() as session:
            return await SqlOpenOpportunityPort(session).list_open(business_id=business_id)


class RequestScopedCampaignProposals:
    """`accept`/`defer`: una sesion, una transaccion (`_ensure_account_entity`
    + `find_live_equivalent` + `save`, todo o nada -- mismo criterio que
    `RequestScopedExperimentProposals`)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def accept(
        self,
        *,
        business_id: BusinessId,
        account_ref: EntityRef,
        candidate_key: str,
        brief: CampaignBrief,
        expected_contribution_delta: Money | None,
        cause_sentence: str,
        now: datetime,
        proposed_by: str | None = None,
    ) -> CampaignProposalOutcome:
        async with self._session_factory() as session:
            outcome = await SqlCampaignProposalPort(session, clock=self._clock).accept(
                business_id=business_id,
                account_ref=account_ref,
                candidate_key=candidate_key,
                brief=brief,
                expected_contribution_delta=expected_contribution_delta,
                cause_sentence=cause_sentence,
                now=now,
                proposed_by=proposed_by,
            )
            await session.commit()
            return outcome

    async def defer(
        self,
        *,
        business_id: BusinessId,
        account_ref: EntityRef,
        candidate_key: str,
        brief: CampaignBrief,
        expected_contribution_delta: Money | None,
        cause_sentence: str,
        now: datetime,
        postpone_until: datetime,
    ) -> str | None:
        async with self._session_factory() as session:
            outcome = await SqlCampaignProposalPort(session, clock=self._clock).defer(
                business_id=business_id,
                account_ref=account_ref,
                candidate_key=candidate_key,
                brief=brief,
                expected_contribution_delta=expected_contribution_delta,
                cause_sentence=cause_sentence,
                now=now,
                postpone_until=postpone_until,
            )
            await session.commit()
            return outcome
