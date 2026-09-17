"""`LiveEconomicsStep`: adaptador real de `EconomicsStepPort` (T156). Por
negocio: un `BuildUnitEconomicsProfile` + `ComputeLagCurve` (por plataforma
con cuenta viva) por producto activo, y un `ComputePlatformDivergence` por
cuenta de plataforma sobre sus campanas.

`MarginInputsPort` real (T132, `offering_economics`, 0029_economics_inputs)
se rellena desde el panel (`PUT /offerings/{id}/economics`): sin esa
entrada del dueño para un producto, `SqlMarginInputsPort.get_margin_inputs`
devuelve `None` y `BuildUnitEconomicsProfile` cae a
`provisional_from_price_only`, el incentivo correcto documentado en
profitability-engine.md §1 ('el motor no propone ninguna subida de
gasto')."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.accounts.domain.platform_account import PlatformAccount
from safent_ads.accounts.infrastructure.sql_repositories import (
    SqlAccountRepository,
    SqlAdEntityRepository,
)
from safent_ads.crm.infrastructure.sql_repositories import SqlLeadAttributionRepository
from safent_ads.economics.application.build_unit_economics_profile import (
    BuildUnitEconomicsProfile,
)
from safent_ads.economics.application.compute_lag_curve import ComputeLagCurve
from safent_ads.economics.application.compute_platform_divergence import ComputePlatformDivergence
from safent_ads.economics.application.errors import OfferingPriceMissingError
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.economics.infrastructure.crm_lag_observation_repository import (
    CrmLagObservationRepository,
)
from safent_ads.economics.infrastructure.offering_economics_sql import SqlMarginInputsPort
from safent_ads.economics.infrastructure.sql_repositories import (
    SqlCalendarEventLookupPort,
    SqlLagCurveRepository,
    SqlOfferingPricePort,
    SqlPlatformDivergenceRepository,
    SqlUnitEconomicsProfileRepository,
)
from safent_ads.metrics.infrastructure.sql_repositories import SqlMetricFactRepository
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId, EntityLevel

logger = structlog.get_logger(__name__)

_SELECT_ACTIVE_OFFERING_IDS = text(
    "SELECT id FROM offerings WHERE business_id = :business_id AND is_active = true"
)


class LiveEconomicsStep:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def run(self, business_id: BusinessId, cycle_id: str, now: datetime) -> None:
        del cycle_id, now
        async with self._session_factory() as session:
            offering_ids = await self._active_offering_ids(session, business_id)
            accounts = await SqlAccountRepository(session).list_for_observation(business_id)
            attributions = SqlLeadAttributionRepository(session)
            await self._fill_unit_economics(session, business_id, offering_ids, attributions)
            await self._fill_lag_curves(session, business_id, offering_ids, attributions, accounts)
            await self._fill_platform_divergence(session, business_id, attributions, accounts)
            await session.commit()

    async def _active_offering_ids(
        self, session: AsyncSession, business_id: BusinessId
    ) -> list[ProductId]:
        result = await session.execute(
            _SELECT_ACTIVE_OFFERING_IDS, {"business_id": business_id.value}
        )
        return [ProductId.parse(str(row["id"])) for row in result.mappings().all()]

    async def _fill_unit_economics(
        self,
        session: AsyncSession,
        business_id: BusinessId,
        offering_ids: list[ProductId],
        attributions: SqlLeadAttributionRepository,
    ) -> None:
        use_case = BuildUnitEconomicsProfile(
            profiles=SqlUnitEconomicsProfileRepository(session),
            margin_inputs=SqlMarginInputsPort(session),
            offering_prices=SqlOfferingPricePort(session),
            calendar_events=SqlCalendarEventLookupPort(session),
            lead_attributions=attributions,
            clock=self._clock,
        )
        for product_id in offering_ids:
            try:
                await use_case.execute(business_id=business_id, product_id=product_id)
            except OfferingPriceMissingError:
                logger.info(
                    "economics_cycle_offering_without_price",
                    business_id=str(business_id),
                    product_id=str(product_id),
                )

    async def _fill_lag_curves(
        self,
        session: AsyncSession,
        business_id: BusinessId,
        offering_ids: list[ProductId],
        attributions: SqlLeadAttributionRepository,
        accounts: Sequence[PlatformAccount],
    ) -> None:
        use_case = ComputeLagCurve(
            CrmLagObservationRepository(attributions, SqlCalendarEventLookupPort(session)),
            SqlLagCurveRepository(session),
            self._clock,
        )
        platforms = {account.account_ref.platform.value for account in accounts}
        for product_id in offering_ids:
            for platform in platforms:
                await use_case.execute(
                    business_id=business_id, product_id=product_id, platform=platform
                )

    async def _fill_platform_divergence(
        self,
        session: AsyncSession,
        business_id: BusinessId,
        attributions: SqlLeadAttributionRepository,
        accounts: Sequence[PlatformAccount],
    ) -> None:
        use_case = ComputePlatformDivergence(
            attributions,
            SqlMetricFactRepository(session),
            SqlPlatformDivergenceRepository(session),
            self._clock,
        )
        entity_repo = SqlAdEntityRepository(session)
        for account in accounts:
            entities = await entity_repo.list_by_account(account.account_ref)
            entity_refs = [
                e.entity_ref for e in entities if e.entity_ref.level == EntityLevel.CAMPAIGN
            ]
            platform_account_id = await self._platform_account_id(session, account)
            if platform_account_id is None:
                continue
            await use_case.execute(
                business_id=business_id,
                platform_account_id=platform_account_id,
                entity_refs=entity_refs,
            )

    async def _platform_account_id(
        self, session: AsyncSession, account: PlatformAccount
    ) -> str | None:
        result = await session.execute(
            text(
                "SELECT id FROM platform_accounts WHERE business_id = :business_id "
                "AND account_ref = :account_ref"
            ),
            {
                "business_id": account.business_id.value,
                "platform": account.account_ref.platform.value,
                "external_account_id": account.account_ref.external_account_id,
                "account_ref": str(account.account_ref),
            },
        )
        row = result.mappings().one_or_none()
        return None if row is None else str(row["id"])
