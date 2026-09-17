"""`Money` en unidades minimas (minor units) + divisa ISO 4217.

Vive en `accounts.domain` en vez de `shared` porque el kernel N0 (plan.md §4)
todavia no lo publica (T005 pendiente en este arbol). Es una copia con alcance
de contexto, deliberada y documentada: cuando `shared.money.Money` exista, la
migracion es un cambio de import, no un rediseno (la forma es identica)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from safent_ads.shared.errors import DomainError

_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


class InvalidMoneyError(DomainError):
    """`minor_units` negativo o `currency` con formato ISO 4217 invalido."""


class CurrencyMismatchError(DomainError):
    """Operacion entre dos `Money` de divisas distintas."""


@dataclass(frozen=True, slots=True)
class Money:
    """Importe monetario exacto: enteros en la unidad minima de la divisa
    (centimos para EUR/USD) para evitar el error de redondeo de `float`."""

    minor_units: int
    currency: str

    def __post_init__(self) -> None:
        if self.minor_units < 0:
            raise InvalidMoneyError(f"minor_units negativo: {self.minor_units}")
        if not _CURRENCY_PATTERN.match(self.currency):
            raise InvalidMoneyError(f"divisa ISO 4217 invalida: {self.currency!r}")

    def __add__(self, other: Money) -> Money:
        self._require_same_currency(other)
        return Money(self.minor_units + other.minor_units, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._require_same_currency(other)
        return Money(self.minor_units - other.minor_units, self.currency)

    def __lt__(self, other: Money) -> bool:
        self._require_same_currency(other)
        return self.minor_units < other.minor_units

    def __le__(self, other: Money) -> bool:
        self._require_same_currency(other)
        return self.minor_units <= other.minor_units

    def _require_same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(f"{self.currency} vs {other.currency}")
