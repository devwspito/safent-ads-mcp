"""`StorageUri`: clave opaca que devuelve `AssetStorePort.put` (creative-port.md).
No es una URL: el activo nunca se referencia por URL libre
(threat-model.md C-11/C-28), solo por esta clave que el propio almacen
resuelve internamente."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SAFE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_./-]+$")


class StorageUriFormatError(ValueError):
    """Clave de almacen con caracteres no permitidos o intento de traversal."""


@dataclass(frozen=True, slots=True)
class StorageUri:
    key: str

    def __post_init__(self) -> None:
        if not self.key or ".." in self.key or self.key.startswith("/"):
            raise StorageUriFormatError(f"clave de almacen invalida: {self.key!r}")
        if not _SAFE_KEY_PATTERN.match(self.key):
            raise StorageUriFormatError(f"clave de almacen invalida: {self.key!r}")

    def __str__(self) -> str:
        return self.key
