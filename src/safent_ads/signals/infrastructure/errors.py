"""Excepciones de infraestructura de `signals`."""

from __future__ import annotations

from safent_ads.shared.errors import InfrastructureError


class UnknownEntityRefError(InfrastructureError):
    """La senal o la anomalia apuntan a una entidad ausente de `ad_entities`.
    El esquema exige el negocio de esa entidad (FK compuesta, C-27): guardar
    a ciegas dejaria una senal sin dueno."""
