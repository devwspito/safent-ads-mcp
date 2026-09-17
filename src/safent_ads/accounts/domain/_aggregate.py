"""Mixin interno: acumula eventos de dominio pendientes de publicar. Un
guion bajo en el nombre del modulo porque no es API publica de `accounts`,
solo un detalle de implementacion compartido por los agregados del contexto."""

from __future__ import annotations

from safent_ads.shared.events import DomainEvent


class _EventRecordingAggregate:
    """Cada agregado que hereda de esta clase debe inicializar
    `self._pending_events: list[DomainEvent] = []` en su `__init__`."""

    _pending_events: list[DomainEvent]

    def _record_event(self, event: DomainEvent) -> None:
        self._pending_events.append(event)

    def pull_events(self) -> list[DomainEvent]:
        """Devuelve los eventos acumulados y vacia la cola (consumo unico,
        evita republicar el mismo evento dos veces)."""
        events, self._pending_events = self._pending_events, []
        return events
