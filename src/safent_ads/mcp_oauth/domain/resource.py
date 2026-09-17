"""`ResourceIndicator` (data-model.md, RFC 8707): la URL canonica de `/mcp`
a la que un token queda atado. Comparacion de cadena exacta, sin
normalizar -- `public_base_url` ya llega normalizado desde
`composition/settings.py` (tasks.md 002 T002), asi que este VO no repite
esa validacion (evita una segunda implementacion de la misma
comprobacion)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResourceIndicator:
    value: str

    @classmethod
    def canonical(cls, public_base_url: str) -> ResourceIndicator:
        return cls(f"{public_base_url}/mcp")

    def __str__(self) -> str:
        return self.value
