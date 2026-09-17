"""`Rate` — tasa adimensional en [0,1] (profitability-engine.md §1: 'tasas
en [0,1]'). Cubre IVA, descuento, refund, cobro y `cvr_lead_to_business_conversion`;
`Theta` anade el suelo 0,20 propio del margen retenido."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from safent_ads.economics.domain.errors import RateOutOfRangeError, ThetaBelowFloorError

THETA_FLOOR = Decimal("0.20")
_DEFAULT_THETA = Decimal("0.35")


@dataclass(frozen=True, slots=True)
class Rate:
    """Tasa en [0,1] representada como fraccion, no porcentaje (`0.21`, no `21`)."""

    value: Decimal

    def __post_init__(self) -> None:
        if not (Decimal("0") <= self.value <= Decimal("1")):
            raise RateOutOfRangeError(f"tasa fuera de [0,1]: {self.value}")

    @classmethod
    def of(cls, value: str | int | float | Decimal) -> Rate:
        return cls(Decimal(str(value)))

    @classmethod
    def zero(cls) -> Rate:
        return cls(Decimal("0"))

    @classmethod
    def one(cls) -> Rate:
        return cls(Decimal("1"))

    @property
    def complement(self) -> Rate:
        """`1 - value`: patron recurrente en las formulas de §1."""
        return Rate(Decimal("1") - self.value)

    def as_float(self) -> float:
        return float(self.value)


@dataclass(frozen=True, slots=True)
class Theta:
    """Margen retenido: suelo 0,20 -- 'comprar a contribucion cero esta
    prohibido' (profitability-engine.md §1)."""

    value: Decimal = _DEFAULT_THETA

    def __post_init__(self) -> None:
        if not (THETA_FLOOR <= self.value <= Decimal("1")):
            raise ThetaBelowFloorError(
                f"theta {self.value} por debajo del suelo {THETA_FLOOR} o fuera de [suelo,1]"
            )

    @classmethod
    def default(cls) -> Theta:
        return cls(_DEFAULT_THETA)

    @property
    def rate(self) -> Rate:
        return Rate(self.value)

    @property
    def complement(self) -> Rate:
        return self.rate.complement
