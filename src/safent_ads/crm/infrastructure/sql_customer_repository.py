"""`SqlCustomerRepository` sobre `customers` (0031_customers, spec 027)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.crm.domain.customer import Customer, CustomerId, CustomerState
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.domain.lead_attribution import AttributionRung
from safent_ads.shared.ids import BusinessId, EntityRef

__all__ = ["SqlCustomerRepository"]

_SELECT_COLUMNS = """
    SELECT id, business_id, identity_digest, entity_ref, attribution_rung,
           first_paid_conversion_at, state, currency, first_seen_at, last_seen_at,
           source_connector_id
      FROM customers
"""

_FIND_BY_IDENTITY = text(
    f"{_SELECT_COLUMNS} WHERE business_id = :business_id AND identity_digest = :identity_digest"
)

_UPSERT = text("""
    INSERT INTO customers (
        id, business_id, identity_digest, salt_version, entity_ref, attribution_rung,
        first_paid_conversion_at, state, currency, first_seen_at, last_seen_at, source_connector_id
    ) VALUES (
        :id, :business_id, :identity_digest, :salt_version, :entity_ref, :attribution_rung,
        :first_paid_conversion_at, :state, :currency, :first_seen_at, :last_seen_at,
        :source_connector_id
    )
    ON CONFLICT ON CONSTRAINT customers_business_identity_unique DO UPDATE SET
        entity_ref = EXCLUDED.entity_ref,
        attribution_rung = EXCLUDED.attribution_rung,
        first_paid_conversion_at = EXCLUDED.first_paid_conversion_at,
        state = EXCLUDED.state,
        last_seen_at = EXCLUDED.last_seen_at,
        source_connector_id = EXCLUDED.source_connector_id
""")

_DELETE_FOR_IDENTITY = text(
    "DELETE FROM customers WHERE business_id = :business_id AND identity_digest = :identity_digest "
    "RETURNING id"
)

_COUNT_ACTIVE_IN_WINDOW = text("""
    SELECT count(*) AS n
      FROM customers
     WHERE business_id = :business_id
       AND first_paid_conversion_at >= :window_start AND first_paid_conversion_at < :window_end
       AND (CAST(:entity_ref AS TEXT) IS NULL OR entity_ref = :entity_ref)
""")

# `salt_version`: `Customer` no lo modela (vive solo en `identity_mappings`,
# data-model.md); se persiste 1 como valor de arranque compatible con el
# CHECK `>= 1` -- `IngestCustomers` escribe el valor real en
# `identity_mappings` por separado (misma fila logica, dos tablas).
_DEFAULT_SALT_VERSION = 1


class SqlCustomerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_identity(
        self, *, business_id: BusinessId, identity_digest: str
    ) -> Customer | None:
        result = await self._session.execute(
            _FIND_BY_IDENTITY,
            {"business_id": business_id.value, "identity_digest": identity_digest},
        )
        row = result.mappings().one_or_none()
        return None if row is None else _row_to_customer(row)

    async def upsert(self, customer: Customer) -> None:
        await self._session.execute(_UPSERT, _customer_params(customer))
        await self._session.flush()

    async def delete_for_identity(self, *, business_id: BusinessId, identity_digest: str) -> int:
        result = await self._session.execute(
            _DELETE_FOR_IDENTITY,
            {"business_id": business_id.value, "identity_digest": identity_digest},
        )
        await self._session.flush()
        return len(result.mappings().all())

    async def count_active_in_window(
        self,
        *,
        business_id: BusinessId,
        entity_ref: str | None,
        window_start: datetime,
        window_end: datetime,
    ) -> int:
        result = await self._session.execute(
            _COUNT_ACTIVE_IN_WINDOW,
            {
                "business_id": business_id.value,
                "entity_ref": entity_ref,
                "window_start": window_start,
                "window_end": window_end,
            },
        )
        return int(result.scalar_one())


def _customer_params(customer: Customer) -> dict[str, object]:
    return {
        "id": customer.customer_id.value,
        "business_id": customer.business_id.value,
        "identity_digest": customer.hashed_identity.digest,
        "salt_version": _DEFAULT_SALT_VERSION,
        "entity_ref": str(customer.entity_ref) if customer.entity_ref is not None else None,
        "attribution_rung": customer.attribution_rung.value,
        "first_paid_conversion_at": customer.first_paid_conversion_at,
        "state": customer.state.value,
        "currency": customer.currency,
        "first_seen_at": customer.first_seen_at,
        "last_seen_at": customer.last_seen_at,
        "source_connector_id": customer.source_connector_id,
    }


def _row_to_customer(row: RowMapping) -> Customer:
    business_id = BusinessId(row["business_id"])
    entity_ref = row["entity_ref"]
    return Customer(
        customer_id=CustomerId(row["id"]),
        business_id=business_id,
        hashed_identity=HashedIdentity.from_digest(
            business_id=business_id, digest=row["identity_digest"]
        ),
        entity_ref=EntityRef.parse(entity_ref) if entity_ref is not None else None,
        attribution_rung=AttributionRung(row["attribution_rung"]),
        first_paid_conversion_at=row["first_paid_conversion_at"],
        state=CustomerState(row["state"]),
        currency=row["currency"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        source_connector_id=row["source_connector_id"],
    )
