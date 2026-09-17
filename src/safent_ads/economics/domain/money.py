"""Value object `Money` (data-model.md `Money = {amount, currency}`).

`shared/` (N0) todavia no expone un `Money` comun entre bounded contexts
(misma constatacion que `proposals/domain/money.py` y `accounts/domain/money.py`,
comprobado 2026-09-09). Copia con alcance de contexto, deliberada: la
economia unitaria trabaja en importes exactos (`Decimal`) porque tasas como
`theta` o `refund_rate` se multiplican en cadena y el redondeo a centimos
solo ocurre al final de cada formula (profitability-engine.md §1). Si
`shared/` incorpora su propio `Money`, la migracion es un cambio de import."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

_ISO_4217_LENGTH = 3
_CENTS = Decimal("0.01")


class CurrencyMismatchError(ValueError):
    """Dos importes de divisas distintas no se pueden combinar."""


@dataclass(frozen=True, slots=True)
class Money:
    """Importe monetario exacto (nunca `float`) con divisa ISO-4217."""

    amount: Decimal
    currency: str = "EUR"

    def __post_init__(self) -> None:
        if len(self.currency) != _ISO_4217_LENGTH or not self.currency.isupper():
            raise ValueError(f"currency debe ser ISO-4217 en mayusculas: {self.currency!r}")

    @classmethod
    def of(cls, amount: str | int | float | Decimal, currency: str = "EUR") -> Money:
        return cls(amount=Decimal(str(amount)), currency=currency)

    @classmethod
    def zero(cls, currency: str = "EUR") -> Money:
        return cls(amount=Decimal("0"), currency=currency)

    def _require_same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(f"{self.currency} != {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._require_same_currency(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._require_same_currency(other)
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __lt__(self, other: Money) -> bool:
        self._require_same_currency(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._require_same_currency(other)
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        self._require_same_currency(other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        self._require_same_currency(other)
        return self.amount >= other.amount

    def scaled_by(self, factor: Decimal | float) -> Money:
        quantized = (self.amount * Decimal(str(factor))).quantize(_CENTS, rounding=ROUND_HALF_UP)
        return Money(quantized, self.currency)

    def is_positive(self) -> bool:
        return self.amount > 0

    def to_canonical(self) -> dict[str, str]:
        return {"amount": str(self.amount), "currency": self.currency}
