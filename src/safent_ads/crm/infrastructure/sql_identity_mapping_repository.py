"""`SqlIdentityMappingRepository` sobre `identity_mappings` (0031_customers,
spec 027): solo-anexable."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.crm.domain.identity_mapping import IdentityMapping
from safent_ads.shared.ids import BusinessId

__all__ = ["SqlIdentityMappingRepository"]

_UPSERT = text("""
    INSERT INTO identity_mappings (
        business_id, identity_digest, salt_version, merged_into, observed_at
    )
    VALUES (:business_id, :identity_digest, :salt_version, :merged_into, :observed_at)
    ON CONFLICT ON CONSTRAINT identity_mappings_unique DO UPDATE SET
        merged_into = EXCLUDED.merged_into, observed_at = EXCLUDED.observed_at
""")

_DELETE_FOR_IDENTITY = text(
    "DELETE FROM identity_mappings "
    "WHERE business_id = :business_id AND identity_digest = :identity_digest "
    "RETURNING id"
)


class SqlIdentityMappingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, mapping: IdentityMapping) -> None:
        await self._session.execute(
            _UPSERT,
            {
                "business_id": mapping.business_id.value,
                "identity_digest": mapping.identity_digest,
                "salt_version": mapping.salt_version,
                "merged_into": mapping.merged_into,
                "observed_at": mapping.observed_at,
            },
        )
        await self._session.flush()

    async def delete_for_identity(self, *, business_id: BusinessId, identity_digest: str) -> int:
        result = await self._session.execute(
            _DELETE_FOR_IDENTITY,
            {"business_id": business_id.value, "identity_digest": identity_digest},
        )
        await self._session.flush()
        return len(result.mappings().all())
