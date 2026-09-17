"""Traduce el `Window` del contrato MCP (`contracts/mcp-tools.md`: o bien
`preset`+`lag_days`, o bien un rango explicito) a un par de fechas SQL.
Compartido por los puertos SQL de esta lane (`portfolio`/`entity`/`signal`)
para no repetir la misma tabla de dias tres veces."""

from __future__ import annotations

from datetime import date, timedelta

from safent_ads.mcp.application.dto import Window, WindowPreset

_PRESET_DAYS: dict[WindowPreset, int] = {
    WindowPreset.TODAY: 1,
    WindowPreset.THREE_DAYS: 3,
    WindowPreset.SEVEN_DAYS: 7,
    WindowPreset.FOURTEEN_DAYS: 14,
    WindowPreset.THIRTY_DAYS: 30,
}


def window_bounds(window: Window, today: date) -> tuple[date, date]:
    """`window` ya llega validado por `presentation/args.py` (nunca ambos
    `preset` y rango explicito a la vez): aqui solo se traduce, no se
    revalida."""
    if window.preset is None:
        assert window.date_from is not None  # noqa: S101 - invariante validado en presentation
        assert window.date_to is not None  # noqa: S101
        return window.date_from, window.date_to
    end = today - timedelta(days=window.lag_days)
    if window.preset is WindowPreset.MONTH_TO_DATE:
        return end.replace(day=1), end
    return end - timedelta(days=_PRESET_DAYS[window.preset] - 1), end
