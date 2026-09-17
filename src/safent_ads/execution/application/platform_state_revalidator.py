"""`PlatformStateRevalidator` (T064) — paso 5 de plan.md §6: leer estado
remoto y comparar con `platform_state_hash`. Divergencia -> `SKIPPED_DRIFT`
(threat-model.md C-16).

Puerto de `oposads/hitl/infrastructure/action_executor.py::PlatformStateRevalidator`:
un fallo de lectura se trata como deriva (nunca se ejecuta a ciegas)."""

from __future__ import annotations

import structlog

from safent_ads.execution.application.ports import PlatformReaderPort
from safent_ads.shared.ids import EntityRef

logger = structlog.get_logger(__name__)


class PlatformStateRevalidator:
    def __init__(self, reader: PlatformReaderPort) -> None:
        self._reader = reader

    async def has_drifted(self, entity_ref: EntityRef, expected_state_hash: str | None) -> bool:
        """Sin `expected_state_hash` capturado (p. ej. `create_campaign`, que
        no tiene estado remoto previo) no hay nada que comparar: no ha
        derivado. Un error de lectura SIEMPRE cuenta como deriva — default
        deny, nunca se ejecuta a ciegas."""
        if expected_state_hash is None:
            return False
        try:
            current_hash = await self._reader.fetch_state_hash(entity_ref)
        except Exception:
            logger.warning("platform_state_revalidation_failed", entity_ref=str(entity_ref))
            return True
        return current_hash != expected_state_hash
