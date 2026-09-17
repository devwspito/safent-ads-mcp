"""Errores de aplicacion de `catalog`."""

from __future__ import annotations

from safent_ads.shared.errors import ApplicationError


class CalendarEventNotFoundError(ApplicationError):
    """`calendar_event_id` no existe para ese `business_id` (o pertenece a
    otro negocio -- el borde HTTP lo traduce a 404, nunca 403, para no
    filtrar existencia entre negocios)."""
