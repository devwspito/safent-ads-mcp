"""`DateWindow`: rango de fechas inclusivo sobre el que se agregan hechos de
metricas (data-model.md: 'ventana de datos')."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from safent_ads.metrics.domain.errors import InvalidDateWindowError


@dataclass(frozen=True, kw_only=True, slots=True)
class DateWindow:
    start_date: date
    end_date: date

    def __post_init__(self) -> None:
        if self.start_date > self.end_date:
            raise InvalidDateWindowError(
                f"start_date posterior a end_date: {self.start_date} > {self.end_date}"
            )

    @classmethod
    def trailing(cls, *, end_date: date, days: int) -> DateWindow:
        """Ventana de `days` dias terminando (incluido) en `end_date`, p. ej.
        3D/7D/14D/30D del catalogo de reglas."""
        return cls(start_date=end_date - timedelta(days=days - 1), end_date=end_date)

    def contains(self, moment: date) -> bool:
        return self.start_date <= moment <= self.end_date

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1
