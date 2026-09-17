"""Sonda real de claves BYOK configuradas (tool-surface.md §6: "Lo que debe
aportar el dueno" -- `FAL_KEY`, `ELEVENLABS_API_KEY`, `BRAVE_API_KEY`).
Unico adaptador de `CapabilityReadPort` que esta lane cablea de verdad: lee
`os.environ`, nunca el valor de la clave, solo si esta presente. Nunca
lanza -- ausencia de una clave es un resultado valido, no un fallo."""

from __future__ import annotations

import os
from collections.abc import Iterable

_KNOWN_BYOK_KEYS: tuple[str, ...] = ("FAL_KEY", "ELEVENLABS_API_KEY", "BRAVE_API_KEY")


class EnvironmentCapabilityProbe:
    def __init__(self, known_keys: Iterable[str] = _KNOWN_BYOK_KEYS) -> None:
        self._known_keys = tuple(known_keys)

    async def configured_byok_keys(self) -> frozenset[str]:
        return frozenset(key for key in self._known_keys if os.environ.get(key))
