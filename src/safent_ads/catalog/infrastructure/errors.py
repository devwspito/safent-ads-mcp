"""Errores de infraestructura de `catalog`."""

from __future__ import annotations

from safent_ads.shared.errors import InfrastructureError


class OfferingNotFoundError(InfrastructureError):
    """`offering_id` no existe (violacion de
    `calendar_events_offering_id_fkey`): el borde HTTP lo traduce a 422
    `VALIDATION_ERROR`, nunca deja escapar el `IntegrityError` crudo."""
