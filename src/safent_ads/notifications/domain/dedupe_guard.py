"""Politica pura de deduplicacion (NFR-6): la version persistida (UNIQUE
`dedupe_key` en la tabla `notifications`, migracion `0010_notifications`,
fuera de esta lane) es la que realmente impide un doble envio en produccion;
esta clase modela el mismo invariante en memoria para que el caso de uso
pueda negarse a reenviar dentro del mismo proceso/ciclo sin tocar
infraestructura, y para que el invariante sea verificable en el dominio."""

from __future__ import annotations

from safent_ads.notifications.domain.value_objects import DedupeKey


class DuplicateSendGuard:
    """Registro en memoria de `dedupe_key` ya enviadas. `register` devuelve
    `True` la primera vez que ve una clave (procede el envio) y `False` en
    cualquier repeticion (el reintento se descarta, nunca duplica)."""

    def __init__(self) -> None:
        self._seen: set[DedupeKey] = set()

    def register(self, dedupe_key: DedupeKey) -> bool:
        if dedupe_key in self._seen:
            return False
        self._seen.add(dedupe_key)
        return True

    def already_sent(self, dedupe_key: DedupeKey) -> bool:
        return dedupe_key in self._seen
