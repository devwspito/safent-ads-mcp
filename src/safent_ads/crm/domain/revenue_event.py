"""`RevenueEvent` (VO solo-anexable, data-model.md §RevenueEvent, spec 027):
primera compra, cobro recurrente, devolucion o baja. Una devolucion es un
hecho de importe NEGATIVO, jamas el borrado del cobro original (FR-019,
"sin reescribir la historia")."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from safent_ads.crm.domain.customer import CustomerId
from safent_ads.crm.domain.errors import (
    ChurnMustNotBePositiveError,
    NonPositiveRevenueAmountError,
    RefundMustBeNegativeError,
    UnsupportedCurrencyError,
)

_ISO_4217_LENGTH = 3


class RevenueEventKind(StrEnum):
    FIRST_PAYMENT = "first_payment"
    RECURRING_PAYMENT = "recurring_payment"
    REFUND = "refund"
    CHURN = "churn"


_REDUCING_KINDS = frozenset({RevenueEventKind.REFUND, RevenueEventKind.CHURN})


@dataclass(frozen=True, slots=True)
class RevenueEventId:
    value: uuid.UUID

    @classmethod
    def new(cls) -> RevenueEventId:
        return cls(uuid.uuid4())

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, kw_only=True, slots=True)
class RevenueEvent:
    revenue_event_id: RevenueEventId
    customer_id: CustomerId
    kind: RevenueEventKind
    amount_minor: int
    currency: str
    occurred_at: datetime
    observed_at: datetime
    source_event_id: str
    mapping_version: int

    def __post_init__(self) -> None:
        _require_valid_currency(self.currency)
        if not self.source_event_id.strip():
            raise ValueError("source_event_id vacio")
        if self.mapping_version < 1:
            raise ValueError(f"mapping_version debe ser >= 1: {self.mapping_version}")
        _require_valid_amount(self.kind, self.amount_minor)

    @property
    def is_reduction(self) -> bool:
        """`refund`/`churn`: resta de la contribucion acumulada, nunca
        borra el hecho original (FR-019)."""
        return self.kind in _REDUCING_KINDS


def _require_valid_currency(currency: str) -> None:
    if len(currency) != _ISO_4217_LENGTH or not currency.isupper():
        raise UnsupportedCurrencyError(f"currency debe ser ISO-4217 en mayusculas: {currency!r}")


def _require_valid_amount(kind: RevenueEventKind, amount_minor: int) -> None:
    if kind is RevenueEventKind.REFUND and amount_minor >= 0:
        raise RefundMustBeNegativeError(f"refund debe ser negativo: {amount_minor}")
    if kind is RevenueEventKind.CHURN and amount_minor > 0:
        raise ChurnMustNotBePositiveError(f"churn no puede ser positivo: {amount_minor}")
    if kind in (RevenueEventKind.FIRST_PAYMENT, RevenueEventKind.RECURRING_PAYMENT) and (
        amount_minor <= 0
    ):
        raise NonPositiveRevenueAmountError(f"{kind.value} debe ser > 0: {amount_minor}")
