"""`SqlCampaignPackageRepository`: implementa `CampaignPackageRepository`
estructuralmente (sin heredar del `Protocol`, mismo criterio que
`SqlProposalRepository`) sobre `campaign_packages` (migracion 0042/0044).

**Recalcula `package_hash` al leer** -- nunca se fia del valor almacenado
(invariante 8, mismo criterio que `SqlProposalRepository` con `diff_hash`):
un bug de escritura o una fila restaurada de una copia no puede hacer que
el servidor firme como si el arbol vivo fuera otro."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import RowMapping, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.packages.application.errors import DuplicateOpenPackageError
from safent_ads.packages.application.ports import PackageListItem
from safent_ads.packages.domain.campaign_package import CampaignPackage, PackageState
from safent_ads.packages.domain.identifiers import OfferingId, PackageGroupId, PackageId
from safent_ads.packages.domain.package_hash import compute_package_hash, package_tree_payload
from safent_ads.packages.infrastructure.package_codec import (
    decode_budget,
    decode_plan,
    decode_publish_as,
    decode_rationale,
    decode_research,
    encode_budget,
    encode_plan,
    encode_publish_as,
    encode_rationale,
    encode_research,
)
from safent_ads.shared.ids import BusinessId, EntityRef, PlatformCode

__all__ = ["SqlCampaignPackageRepository"]

_INSERT_SQL = text("""
    INSERT INTO campaign_packages
        (id, business_id, platform, account_ref, offering_id, package_group_id, state,
         package_hash, plan, budget, rationale, research, publish_as, owner_context,
         created_at, expires_at)
    VALUES
        (:id, :business_id, :platform, :account_ref, :offering_id, :package_group_id, :state,
         :package_hash, CAST(:plan AS JSONB), CAST(:budget AS JSONB), CAST(:rationale AS JSONB),
         CAST(:research AS JSONB), CAST(:publish_as AS JSONB), :owner_context,
         :created_at, :expires_at)
""")

_UPDATE_SQL = text("""
    UPDATE campaign_packages
       SET state = :state, package_hash = :package_hash, plan = CAST(:plan AS JSONB),
           owner_context = :owner_context
     WHERE id = :id AND business_id = :business_id
""")

_GET_SQL = text("""
    SELECT id, business_id, platform, account_ref, offering_id, package_group_id, state,
           plan::text AS plan_text, budget::text AS budget_text,
           rationale::text AS rationale_text, research::text AS research_text,
           publish_as::text AS publish_as_text, owner_context, created_at, expires_at
      FROM campaign_packages
     WHERE id = :id AND business_id = :business_id
""")

_FIND_OPEN_DUPLICATE_SQL = text("""
    SELECT id FROM campaign_packages
     WHERE business_id = :business_id AND account_ref = :account_ref
       AND offering_id = :offering_id AND state IN ('draft', 'proposed')
     LIMIT 1
""")

_LIST_SQL = text("""
    SELECT cp.id, cp.state, cp.platform, pa.external_account_id AS account_name,
           cp.plan::text AS plan_text, cp.budget::text AS budget_text,
           cp.created_at, cp.expires_at
      FROM campaign_packages AS cp
      LEFT JOIN platform_accounts AS pa
        ON pa.business_id = cp.business_id AND pa.account_ref = cp.account_ref
     WHERE cp.business_id = :business_id
       AND (CAST(:state AS TEXT) IS NULL OR cp.state = :state)
       AND (CAST(:cursor AS TIMESTAMPTZ) IS NULL OR cp.created_at < :cursor)
     ORDER BY cp.created_at DESC
     LIMIT :limit
""")


class SqlCampaignPackageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, package: CampaignPackage) -> None:
        try:
            await self._session.execute(_INSERT_SQL, _params(package))
        except IntegrityError as exc:
            if "ix_campaign_packages_open_dedup" in str(exc.orig):
                raise DuplicateOpenPackageError(package_id="") from exc
            raise

    async def save(self, package: CampaignPackage) -> None:
        await self._session.execute(
            _UPDATE_SQL,
            {
                "id": str(package.package_id),
                "business_id": str(package.business_id),
                "state": package.state.value,
                "package_hash": package.package_hash.value,
                "plan": encode_plan(package.campaign, package.ad_sets),
                "owner_context": package.owner_context,
            },
        )

    async def get(
        self, package_id: PackageId, *, business_id: BusinessId
    ) -> CampaignPackage | None:
        result = await self._session.execute(
            _GET_SQL, {"id": str(package_id), "business_id": str(business_id)}
        )
        row = result.mappings().one_or_none()
        return None if row is None else _row_to_package(row)

    async def find_open_duplicate(
        self, *, business_id: BusinessId, account_ref: EntityRef, offering_id: OfferingId
    ) -> PackageId | None:
        result = await self._session.execute(
            _FIND_OPEN_DUPLICATE_SQL,
            {
                "business_id": str(business_id),
                "account_ref": str(account_ref),
                "offering_id": uuid.UUID(str(offering_id)),
            },
        )
        row = result.scalar_one_or_none()
        return None if row is None else PackageId.parse(str(row))

    async def list_for_business(
        self,
        *,
        business_id: BusinessId,
        state: PackageState | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[tuple[PackageListItem, ...], str | None]:
        result = await self._session.execute(
            _LIST_SQL,
            {
                "business_id": str(business_id),
                "state": state.value if state is not None else None,
                "cursor": datetime.fromisoformat(cursor) if cursor is not None else None,
                "limit": limit + 1,
            },
        )
        rows = result.mappings().all()
        has_more = len(rows) > limit
        page = rows[:limit]
        items = tuple(_row_to_list_item(row) for row in page)
        next_cursor = page[-1]["created_at"].isoformat() if has_more and page else None
        return items, next_cursor


def _params(package: CampaignPackage) -> dict[str, object]:
    return {
        "id": str(package.package_id),
        "business_id": str(package.business_id),
        "platform": package.account_ref.platform.value,
        "account_ref": str(package.account_ref),
        "offering_id": uuid.UUID(str(package.offering_id)),
        "package_group_id": (
            str(package.package_group_id) if package.package_group_id is not None else None
        ),
        "state": package.state.value,
        "package_hash": package.package_hash.value,
        "plan": encode_plan(package.campaign, package.ad_sets),
        "budget": encode_budget(package.budget),
        "rationale": encode_rationale(package.rationale),
        "research": encode_research(package.research),
        "publish_as": encode_publish_as(package.publish_as),
        "owner_context": package.owner_context,
        "created_at": package.created_at,
        "expires_at": package.expires_at,
    }


def _row_to_package(row: RowMapping) -> CampaignPackage:
    campaign, ad_sets = decode_plan(row["plan_text"])
    budget = decode_budget(row["budget_text"])
    rationale = decode_rationale(row["rationale_text"])
    research = decode_research(row["research_text"])
    publish_as = decode_publish_as(row["publish_as_text"])
    account_ref = EntityRef.parse(row["account_ref"])
    business_id = BusinessId.parse(str(row["business_id"]))
    offering_id = OfferingId(str(row["offering_id"]))
    package_hash = compute_package_hash(
        package_tree_payload(
            business_id=business_id,
            platform=account_ref.platform,
            account_ref=account_ref,
            publish_as=publish_as,
            offering_id=offering_id,
            campaign=campaign,
            ad_sets=ad_sets,
            daily_budget=budget.daily,
            rationale=rationale,
            research=research,
        )
    )
    package_group_id_value = row["package_group_id"]
    return CampaignPackage(
        package_id=PackageId.parse(str(row["id"])),
        business_id=business_id,
        account_ref=account_ref,
        publish_as=publish_as,
        offering_id=offering_id,
        campaign=campaign,
        ad_sets=ad_sets,
        budget=budget,
        rationale=rationale,
        research=research,
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        package_hash=package_hash,
        state=PackageState(row["state"]),
        package_group_id=(
            PackageGroupId(str(package_group_id_value))
            if package_group_id_value is not None
            else None
        ),
        owner_context=row["owner_context"],
    )


def _row_to_list_item(row: RowMapping) -> PackageListItem:
    campaign, ad_sets = decode_plan(row["plan_text"])
    budget = decode_budget(row["budget_text"])
    return PackageListItem(
        package_id=PackageId.parse(str(row["id"])),
        state=PackageState(row["state"]),
        platform=PlatformCode(row["platform"]),
        account_name=row["account_name"],
        campaign_name=campaign.name,
        ads_count=sum(len(ad_set.ads) for ad_set in ad_sets),
        daily_budget=budget.daily,
        total_cap=budget.total_cap,
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )
