"""`DateWindow`: rango de fechas inclusivo usado para pedir metricas
(`AdsPlatformPort.fetch_metrics`). Igual que `Money` (ver `money.py`), vive
aqui porque el kernel N0 (plan.md §4) todavia no publica `shared.DateWindow`
en este arbol; migrar es un cambio de import."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from safent_ads.shared.errors import DomainError


class InvalidDateWindowError(DomainError):
    """`start` posterior a `end`."""


@dataclass(frozen=True, slots=True)
class DateWindow:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise InvalidDateWindowError(f"start {self.start} posterior a end {self.end}")
