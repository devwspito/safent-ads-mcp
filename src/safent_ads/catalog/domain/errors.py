"""Errores de dominio de `catalog` (data-model.md §CalendarEvent,
generalizado por vocabulary.md T175). Nombrados por invariante violado,
nunca genericos."""

from __future__ import annotations

from safent_ads.shared.errors import DomainError


class InvalidCalendarEventWindowError(DomainError):
    """`window_start >= window_end`: una ventana vacia o invertida no
    representa ningun hito real."""


class InvalidEventDateError(DomainError):
    """`event_date < window_start`: el hito no puede caer antes de que la
    ventana que lo motiva se abra."""


class InvalidCalendarEventNameError(DomainError):
    """`name` fuera de 1-120 caracteres (contracts/rest-api.md §Calendario)."""


class InvalidRegionError(DomainError):
    """`region`, cuando esta presente, fuera de 2-64 caracteres."""
