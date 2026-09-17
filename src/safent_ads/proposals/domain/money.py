"""Value object `Money` (data-model.md `Money = {amount, currency}`).

`shared/` (N0) todavia no expone un `Money` comun entre bounded contexts —
ninguna otra rama lo ha definido aun (comprobado 2026-09-09). Se define aqui,
en la capa mas baja de la rama `us2` (`proposals`), para que `execution`
(N6, que depende de `proposals` N5 en plan.md §4) lo reutilice sin duplicar
la aritmetica de guardarrailes. Si `shared/` incorpora su propio `Money` en
otra rama, este modulo se sustituye por un re-export sin tocar el resto de
`proposals`/`execution` (misma forma: `amount: Decimal`, `currency: str`)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

_ISO_4217_LENGTH = 3


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

    def scaled_by(self, factor: Decimal) -> Money:
        quantized = (self.amount * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return Money(quantized, self.currency)

    def is_positive(self) -> bool:
        return self.amount > 0

    def to_canonical(self) -> dict[str, str]:
        return {"amount": str(self.amount), "currency": self.currency}
