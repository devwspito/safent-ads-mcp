"""`ForgetCustomer` (spec 027 A-2, contracts/crm-link.md §2 `POST
/crm/customers/forget`): borra por digest en las tres tablas dentro de una
transaccion y anota la supresion sin el identificador crudo -- el crudo
nunca existio aqui, asi que no hay nada que redactar."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.crm.application.customer_ports import (
    CustomerForgottenRecorder,
    CustomerRepository,
    IdentityMappingRepository,
    RevenueEventRepository,
)
from safent_ads.crm.domain.identity_mapping import require_hashed_digest
from safent_ads.shared.ids import BusinessId


@dataclass(frozen=True, kw_only=True, slots=True)
class ForgetCustomerResult:
    rows_deleted: int
    recorded: bool = True


class ForgetCustomer:
    def __init__(
        self,
        *,
        customers: CustomerRepository,
        revenue_events: RevenueEventRepository,
        identity_mappings: IdentityMappingRepository,
        audit: CustomerForgottenRecorder,
    ) -> None:
        self._customers = customers
        self._revenue_events = revenue_events
        self._identity_mappings = identity_mappings
        self._audit = audit

    async def execute(
        self, *, business_id: BusinessId, identity_digest: str
    ) -> ForgetCustomerResult:
        require_hashed_digest(identity_digest)
        customer = await self._customers.find_by_identity(
            business_id=business_id, identity_digest=identity_digest
        )
        revenue_rows = (
            await self._revenue_events.delete_for_customer(
                business_id=business_id, customer_id=customer.customer_id
            )
            if customer is not None
            else 0
        )
        mapping_rows = await self._identity_mappings.delete_for_identity(
            business_id=business_id, identity_digest=identity_digest
        )
        customer_rows = await self._customers.delete_for_identity(
            business_id=business_id, identity_digest=identity_digest
        )
        rows_deleted = revenue_rows + mapping_rows + customer_rows
        await self._audit.record(
            business_id=business_id, customer_hash=identity_digest, rows_deleted=rows_deleted
        )
        return ForgetCustomerResult(rows_deleted=rows_deleted)
