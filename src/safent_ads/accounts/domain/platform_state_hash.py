"""`PlatformStateHash`: sha256 sobre los campos remotos canonicos de una
entidad. data-model.md: "`platform_state_hash` se recalcula en cada lectura
remota - si difiere del almacenado, la entidad queda `drifted`"."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from safent_ads.accounts.domain.json_value import JsonValue
from safent_ads.shared.errors import DomainError

_HEX_SHA256_LENGTH: Final = 64


class InvalidPlatformStateHashError(DomainError):
    """`value` no es un digest sha256 hexadecimal de 64 caracteres."""


@dataclass(frozen=True, slots=True)
class PlatformStateHash:
    """Huella de estado remoto. Solo se construye a mano en tests; en
    produccion siempre via `compute`."""

    value: str

    def __post_init__(self) -> None:
        if len(self.value) != _HEX_SHA256_LENGTH or not _is_lowercase_hex(self.value):
            raise InvalidPlatformStateHashError(f"hash sha256 invalido: {self.value!r}")

    @classmethod
    def compute(cls, canonical_fields: Mapping[str, JsonValue]) -> PlatformStateHash:
        """Canonicaliza `canonical_fields` (claves ordenadas, sin espacios) y
        calcula su sha256. El llamante decide que campos remotos son
        significativos para la deriva; esta funcion no filtra ninguno."""
        canonical_json = json.dumps(canonical_fields, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        return cls(digest)


def _is_lowercase_hex(value: str) -> bool:
    return all(char in "0123456789abcdef" for char in value)
