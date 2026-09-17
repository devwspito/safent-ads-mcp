"""`entity_capabilities` (design.md §0.5-0.7): que
puede hacer el propietario con una entidad desde el panel -- pausar,
reanudar, borrar -- SIN abrir una propuesta (`execution.application.
entity_lifecycle_actions.PauseEntity`/`ResumeEntity`/`DeleteEntity` son las
rutas ya existentes que estos indicadores anuncian). Puro, sin I/O: mismo
criterio que `row_action.py`."""

from __future__ import annotations

from dataclasses import dataclass

_ACTIVE_STATUS = "active"
_PAUSED_STATUS = "paused"
_REMOVED_STATUS = "removed"


@dataclass(frozen=True, slots=True)
class EntityCapabilities:
    can_pause: bool
    can_resume: bool
    can_delete: bool


def entity_capabilities(*, status: str, is_controllable: bool) -> EntityCapabilities:
    normalized = status.lower()
    return EntityCapabilities(
        can_pause=is_controllable and normalized == _ACTIVE_STATUS,
        can_resume=is_controllable and normalized == _PAUSED_STATUS,
        can_delete=is_controllable and normalized != _REMOVED_STATUS,
    )
