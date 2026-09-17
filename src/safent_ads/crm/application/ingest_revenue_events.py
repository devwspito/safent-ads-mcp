"""`IngestRevenueEvents` (spec 027 T016, contracts/crm-link.md §2 `POST
/crm/revenue-events`): fila a fila, nunca todo o nada (mismo criterio que
`ImportConversionsFromCsv`) -- un hecho mal formado se rechaza con su
motivo, el resto del lote se sigue procesando. Idempotente por esquema
(`RevenueEventRepository.insert_if_new`), nunca por codigo."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from safent_ads.crm.application.customer_ports import CustomerRepository, RevenueEventRepository
from safent_ads.crm.domain.customer import Customer
from safent_ads.crm.domain.errors import (
    ChurnMustNotBePositiveError,
    NonPositiveRevenueAmountError,
    RefundMustBeNegativeError,
    UnsupportedCurrencyError,
)
from safent_ads.crm.domain.identity_mapping import require_hashed_digest
from safent_ads.crm.domain.revenue_event import RevenueEvent, RevenueEventId, RevenueEventKind
from safent_ads.shared.errors import ApplicationError
from safent_ads.shared.ids import BusinessId

_MAX_BATCH_SIZE = 1_000
_REDUCING_KINDS = frozenset({RevenueEventKind.REFUND, RevenueEventKind.CHURN})
_FIRST_PAYMENT_KINDS = frozenset(
    {RevenueEventKind.FIRST_PAYMENT, RevenueEventKind.RECURRING_PAYMENT}
)


class RevenueEventBatchTooLargeError(ApplicationError):
    """Mas de 1 000 items en un lote (contracts/crm-link.md §2)."""


class CustomerNotFoundForRevenueEventError(ApplicationError):
    """El hecho llega sin un `Customer` conocido para su `identity_digest`
    -- el puente debe llamar `POST /crm/customers` primero."""


@dataclass(frozen=True, kw_only=True, slots=True)
class RevenueEventIngestItem:
    source_event_id: str
    identity_digest: str
    kind: str
    amount_minor: int
    currency: str
    occurred_at: datetime
    observed_at: datetime
    mapping_version: int


@dataclass(frozen=True, kw_only=True, slots=True)
class RejectedRevenueEventItem:
    source_event_id: str
    code: str


@dataclass(frozen=True, kw_only=True, slots=True)
class RevenueEventIngestResult:
    ingested: int
    duplicated: int
    rejected: tuple[RejectedRevenueEventItem, ...]


class IngestRevenueEvents:
    def __init__(
        self, *, customers: CustomerRepository, revenue_events: RevenueEventRepository
    ) -> None:
        self._customers = customers
        self._revenue_events = revenue_events

    async def execute(
        self,
        *,
        business_id: BusinessId,
        connector_id: str,
        items: Sequence[RevenueEventIngestItem],
    ) -> RevenueEventIngestResult:
        if len(items) > _MAX_BATCH_SIZE:
            raise RevenueEventBatchTooLargeError(f"lote de {len(items)} supera {_MAX_BATCH_SIZE}")
        ingested = duplicated = 0
        rejected: list[RejectedRevenueEventItem] = []
        for item in items:
            try:
                is_new = await self._ingest_one(
                    business_id=business_id, connector_id=connector_id, item=item
                )
            except (
                CustomerNotFoundForRevenueEventError,
                RefundMustBeNegativeError,
                ChurnMustNotBePositiveError,
                NonPositiveRevenueAmountError,
                UnsupportedCurrencyError,
                ValueError,
            ) as exc:
                rejected.append(
                    RejectedRevenueEventItem(
                        source_event_id=item.source_event_id, code=_error_code(exc)
                    )
                )
                continue
            if is_new:
                ingested += 1
            else:
                duplicated += 1
        return RevenueEventIngestResult(
            ingested=ingested, duplicated=duplicated, rejected=tuple(rejected)
        )

    async def _ingest_one(
        self, *, business_id: BusinessId, connector_id: str, item: RevenueEventIngestItem
    ) -> bool:
        require_hashed_digest(item.identity_digest)
        customer = await self._customers.find_by_identity(
            business_id=business_id, identity_digest=item.identity_digest
        )
        if customer is None:
            raise CustomerNotFoundForRevenueEventError(item.identity_digest)
        if item.currency != customer.currency:
            raise UnsupportedCurrencyError(
                f"currency {item.currency!r} no coincide con la del negocio {customer.currency!r}"
            )

        kind = RevenueEventKind(item.kind)
        event = RevenueEvent(
            revenue_event_id=RevenueEventId.new(),
            customer_id=customer.customer_id,
            kind=kind,
            amount_minor=item.amount_minor,
            currency=item.currency,
            occurred_at=item.occurred_at,
            observed_at=item.observed_at,
            source_event_id=item.source_event_id,
            mapping_version=item.mapping_version,
        )
        is_new = await self._revenue_events.insert_if_new(
            event, business_id=business_id, connector_id=connector_id
        )
        if is_new:
            await self._customers.upsert(_apply_event(customer, event))
        return is_new


def _apply_event(customer: Customer, event: RevenueEvent) -> Customer:
    if event.kind in _FIRST_PAYMENT_KINDS:
        return customer.record_paid_event(occurred_at=event.occurred_at)
    if event.kind is RevenueEventKind.CHURN:
        return customer.record_churn(occurred_at=event.occurred_at)
    return customer.touch(seen_at=event.occurred_at)


def _error_code(exc: Exception) -> str:
    if isinstance(exc, RefundMustBeNegativeError):
        return "REFUND_MUST_BE_NEGATIVE"
    if isinstance(exc, UnsupportedCurrencyError):
        return "CURRENCY_MISMATCH"
    if isinstance(exc, CustomerNotFoundForRevenueEventError):
        return "CUSTOMER_NOT_FOUND"
    return "VALIDATION_ERROR"
