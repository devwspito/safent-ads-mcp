"""`IngestCustomers` (spec 027 T016, contracts/crm-link.md §2 `POST
/crm/customers`): el puente entrega identidades ya hasheadas en el borde
-- este caso de uso nunca ve, ni podria ver, un dato personal crudo."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from safent_ads.crm.application.customer_ports import CustomerRepository, IdentityMappingRepository
from safent_ads.crm.domain.customer import Customer
from safent_ads.crm.domain.errors import InvalidIdentityDigestError
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.domain.identity_mapping import IdentityMapping, require_hashed_digest
from safent_ads.crm.domain.lead_attribution import AttributionRung
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import ApplicationError
from safent_ads.shared.ids import BusinessId, EntityRef, EntityRefFormatError

_MAX_BATCH_SIZE = 1_000


class CustomerBatchTooLargeError(ApplicationError):
    """Mas de 1 000 items en un lote (contracts/crm-link.md §2)."""


@dataclass(frozen=True, kw_only=True, slots=True)
class CustomerIngestItem:
    identity_digest: str
    salt_version: int
    entity_ref: str | None
    attribution_rung: str
    currency: str = "EUR"


@dataclass(frozen=True, kw_only=True, slots=True)
class RejectedItem:
    index: int
    code: str


@dataclass(frozen=True, kw_only=True, slots=True)
class CustomerIngestResult:
    accepted: int
    merged: int
    rejected: tuple[RejectedItem, ...]


class IngestCustomers:
    def __init__(
        self,
        *,
        customers: CustomerRepository,
        identity_mappings: IdentityMappingRepository,
        clock: Clock,
    ) -> None:
        self._customers = customers
        self._identity_mappings = identity_mappings
        self._clock = clock

    async def execute(
        self,
        *,
        business_id: BusinessId,
        connector_id: str,
        items: Sequence[CustomerIngestItem],
    ) -> CustomerIngestResult:
        if len(items) > _MAX_BATCH_SIZE:
            raise CustomerBatchTooLargeError(f"lote de {len(items)} supera {_MAX_BATCH_SIZE}")
        accepted = merged = 0
        rejected: list[RejectedItem] = []
        seen_at = self._clock.now()
        for index, item in enumerate(items):
            try:
                is_new = await self._ingest_one(
                    business_id=business_id, connector_id=connector_id, item=item, seen_at=seen_at
                )
            except (InvalidIdentityDigestError, EntityRefFormatError, ValueError) as exc:
                rejected.append(RejectedItem(index=index, code=_error_code(exc)))
                continue
            if is_new:
                accepted += 1
            else:
                merged += 1
        return CustomerIngestResult(accepted=accepted, merged=merged, rejected=tuple(rejected))

    async def _ingest_one(
        self,
        *,
        business_id: BusinessId,
        connector_id: str,
        item: CustomerIngestItem,
        seen_at: datetime,
    ) -> bool:
        require_hashed_digest(item.identity_digest)
        entity_ref = EntityRef.parse(item.entity_ref) if item.entity_ref else None
        attribution_rung = AttributionRung(item.attribution_rung)
        identity = HashedIdentity.from_digest(business_id=business_id, digest=item.identity_digest)

        existing = await self._customers.find_by_identity(
            business_id=business_id, identity_digest=item.identity_digest
        )
        if existing is None:
            await self._customers.upsert(
                Customer.first_seen(
                    business_id=business_id,
                    hashed_identity=identity,
                    entity_ref=entity_ref,
                    attribution_rung=attribution_rung,
                    currency=item.currency,
                    seen_at=seen_at,
                    source_connector_id=connector_id,
                )
            )
            is_new = True
        else:
            await self._customers.upsert(
                existing.touch(seen_at=seen_at, source_connector_id=connector_id)
            )
            is_new = False

        await self._identity_mappings.record(
            IdentityMapping(
                business_id=business_id,
                identity_digest=item.identity_digest,
                salt_version=item.salt_version,
                observed_at=seen_at,
            )
        )
        return is_new


def _error_code(exc: Exception) -> str:
    if isinstance(exc, InvalidIdentityDigestError):
        return "IDENTITY_NOT_HASHED"
    if isinstance(exc, EntityRefFormatError):
        return "INVALID_ENTITY_REF"
    return "VALIDATION_ERROR"
