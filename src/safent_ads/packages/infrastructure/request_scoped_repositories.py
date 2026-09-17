"""Cablea los puertos de `packages` que necesita el MCP (`propose_campaign_package`,
sin sesion por peticion de FastAPI) a Postgres real: una sesion por llamada
via `session_factory`, mismo patron que
`opportunities.infrastructure.request_scoped_repositories` -- `composition/app.py`
construye estos envoltorios UNA vez al arrancar la app."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.packages.application.ports import PackageListItem
from safent_ads.packages.domain.campaign_package import CampaignPackage, PackageState
from safent_ads.packages.domain.identifiers import OfferingId, PackageId
from safent_ads.packages.infrastructure.landing_domain_policy import SqlLandingDomainPolicy
from safent_ads.packages.infrastructure.sql_active_account_lookup import (
    SqlAccountDailyCap,
    SqlActiveAccountLookup,
)
from safent_ads.packages.infrastructure.sql_offering_exists import SqlOfferingExists
from safent_ads.packages.infrastructure.sql_package_repository import SqlCampaignPackageRepository
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import BusinessId, EntityRef, PlatformCode

__all__ = [
    "RequestScopedAccountDailyCap",
    "RequestScopedActiveAccountLookup",
    "RequestScopedCampaignPackages",
    "RequestScopedLandingDomainPolicy",
    "RequestScopedOfferingExists",
]


class RequestScopedCampaignPackages:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, package: CampaignPackage) -> None:
        async with self._session_factory() as session:
            await SqlCampaignPackageRepository(session).add(package)
            await session.commit()

    async def save(self, package: CampaignPackage) -> None:
        async with self._session_factory() as session:
            await SqlCampaignPackageRepository(session).save(package)
            await session.commit()

    async def get(
        self, package_id: PackageId, *, business_id: BusinessId
    ) -> CampaignPackage | None:
        async with self._session_factory() as session:
            return await SqlCampaignPackageRepository(session).get(
                package_id, business_id=business_id
            )

    async def find_open_duplicate(
        self, *, business_id: BusinessId, account_ref: EntityRef, offering_id: OfferingId
    ) -> PackageId | None:
        async with self._session_factory() as session:
            return await SqlCampaignPackageRepository(session).find_open_duplicate(
                business_id=business_id, account_ref=account_ref, offering_id=offering_id
            )

    async def list_for_business(
        self,
        *,
        business_id: BusinessId,
        state: PackageState | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[tuple[PackageListItem, ...], str | None]:
        async with self._session_factory() as session:
            return await SqlCampaignPackageRepository(session).list_for_business(
                business_id=business_id, state=state, limit=limit, cursor=cursor
            )


class RequestScopedOfferingExists:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def exists(self, *, business_id: BusinessId, offering_id: str) -> bool:
        async with self._session_factory() as session:
            return await SqlOfferingExists(session).exists(
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
            return await SqlActiveAccountLookup(session).find_active_account(
                business_id=business_id, platform=platform, account_ref=account_ref
            )


class RequestScopedAccountDailyCap:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_daily_cap(self, *, account_ref: EntityRef) -> Money | None:
        async with self._session_factory() as session:
            return await SqlAccountDailyCap(session).get_daily_cap(account_ref=account_ref)


class RequestScopedLandingDomainPolicy:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def allowed_hosts(self, *, business_id: BusinessId) -> frozenset[str]:
        async with self._session_factory() as session:
            return await SqlLandingDomainPolicy(session).allowed_hosts(business_id=business_id)
