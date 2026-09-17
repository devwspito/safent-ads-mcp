"""Formas de lectura byte-identicas antes de esta extraccion entre
`panel/application/dto.py` y `mcp/application/dto.py` (simplification-audit.md
#9: `Money`, `Caps`, `CapsSource`, `SpendBreakdown`, `SignalOutcome`,
`SignalOutcomeStatus` verificados campo-a-campo; `Freshness`/`FreshnessInfo`
y `Pacing`/`PacingOverview` con el mismo conjunto de campos bajo nombres de
clase distintos). Puros: dataclasses/enums, sin I/O, sin logica de negocio
-- `panel` y `mcp` importan desde aqui en vez de redeclarar (plan.md §4)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from safent_ads.shared.errors import DomainError


@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: str = "EUR"


@dataclass(frozen=True, slots=True)
class Freshness:
    last_ingested_at: datetime
    lag_minutes: int
    is_stale: bool
    # Additive (hotfix 0.2.20): an account/business that never ingested
    # anything is not "stale data" -- it is the absence of data. Existing
    # callers (e.g. the MCP STALE_DATA guardrail) never set this and keep
    # treating "never ingested" as `is_stale=True`, unchanged.
    no_data: bool = False


class CapsSource(StrEnum):
    GUARDRAIL = "guardrail"
    BROKER_HARD_CAP = "broker_hard_cap"


@dataclass(frozen=True, slots=True)
class Caps:
    daily: Money | None
    monthly: Money | None
    source: CapsSource


@dataclass(frozen=True, slots=True)
class Pacing:
    index_pct: float
    projection_pct: float
    days_remaining: int


@dataclass(frozen=True, slots=True)
class SpendBreakdown:
    window: Money
    today: Money
    mtd: Money


class SignalOutcomeStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    CONFIRMED = "confirmed"
    NOT_CONFIRMED = "not_confirmed"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class SignalOutcome:
    status: SignalOutcomeStatus
    days_remaining: int | None
    evaluated_at: datetime | None


class MeasureStatus(StrEnum):
    """026, contracts/cockpit-read-model.md §2: el estado de una celda del
    cuadro de mando. `AVAILABLE` es el unico que lleva numero -- el resto
    declara por que no lo hay, nunca un cero (FR-009/SC-006)."""

    AVAILABLE = "available"
    NO_DATA = "no_data"
    INSUFFICIENT_VOLUME = "insufficient_volume"
    IMMATURE_WINDOW = "immature_window"
    LEARNING = "learning"
    NOT_CONTROLLABLE = "not_controllable"
    NO_CUSTOMER_SOURCE = "no_customer_source"
    STALE = "stale"


class MeasureInvariantError(DomainError):
    """Un `Measure` `available` sin valor, o uno no-`available` con un valor
    o sin motivo -- hace irrepresentable la forma "numero suelto" que
    FR-009 prohibe."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Measure[T]:
    """Envoltorio obligatorio de toda celda numerica del cockpit (026,
    contracts/cockpit-read-model.md §2). Construir uno fuera de
    `available()`/`unavailable()` es un error de programacion deliberado:
    esas dos son las UNICAS formas validas, y cada una impone la mitad del
    invariante que la otra prohibe."""

    status: MeasureStatus
    value: T | None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status is MeasureStatus.AVAILABLE:
            if self.value is None:
                raise MeasureInvariantError("Measure available sin value")
            if self.reason is not None:
                raise MeasureInvariantError("Measure available no lleva reason")
        elif self.value is not None:
            raise MeasureInvariantError(f"Measure {self.status} no puede llevar value")
        elif not self.reason:
            raise MeasureInvariantError(f"Measure {self.status} exige reason")

    @classmethod
    def available(cls, value: T) -> Measure[T]:
        return cls(status=MeasureStatus.AVAILABLE, value=value)

    @classmethod
    def unavailable(cls, status: MeasureStatus, *, reason: str) -> Measure[T]:
        if status is MeasureStatus.AVAILABLE:
            raise MeasureInvariantError("usa Measure.available() para status=available")
        return cls(status=status, value=None, reason=reason)
