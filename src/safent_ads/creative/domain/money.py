"""`Money` local a `creative` (plan.md N0 declara `Money` como VO del kernel
`shared`, pero `shared/` todavia no lo publica en este snapshot y esta fuera
del alcance de este carril). Misma forma que se espera del VO compartido
(importe decimal + divisa ISO 4217) para que sustituirlo por un `import`
cuando `shared.money` aterrice sea un cambio mecanico."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

_ISO_4217_LEN = 3


class CurrencyMismatchError(ValueError):
    """Operacion entre dos `Money` de divisas distintas."""


@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if len(self.currency) != _ISO_4217_LEN or not self.currency.isupper():
            raise ValueError(f"divisa invalida, se esperaba ISO 4217: {self.currency!r}")

    def __add__(self, other: Money) -> Money:
        self._require_same_currency(other)
        return Money(self.amount + other.amount, self.currency)

    def __gt__(self, other: Money) -> bool:
        self._require_same_currency(other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        self._require_same_currency(other)
        return self.amount >= other.amount

    def _require_same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(f"{self.currency} != {other.currency}")

    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(Decimal("0"), currency)
