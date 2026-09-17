"""`WindowSpan`: ventanas de datos del catalogo de reglas (rule-catalog-and-
signals.md §2: 'T = hoy, 3D/7D/14D/30D = ultimos N dias incl. hoy')."""

from __future__ import annotations

from enum import StrEnum


class WindowSpan(StrEnum):
    D3 = "3d"
    D7 = "7d"
    D14 = "14d"
    D30 = "30d"
