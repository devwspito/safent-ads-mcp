"""`IdentityMapping` (entidad, data-model.md §IdentityMapping, spec 027):
solo-anexable. Fundir dos identidades escribe `merged_into`, nunca borra la
absorbida -- el mismo cliente visto por dos vias queda trazable."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from safent_ads.crm.domain.errors import InvalidIdentityDigestError
from safent_ads.shared.ids import BusinessId

_SHA256_HEX_LENGTH = 64


@dataclass(frozen=True, kw_only=True, slots=True)
class IdentityMapping:
    business_id: BusinessId
    identity_digest: str
    salt_version: int
    observed_at: datetime
    merged_into: str | None = None

    def __post_init__(self) -> None:
        require_hashed_digest(self.identity_digest)
        if self.merged_into is not None:
            require_hashed_digest(self.merged_into)
        if self.salt_version < 1:
            raise ValueError(f"salt_version debe ser >= 1: {self.salt_version}")

    def merge_into(self, *, surviving_digest: str, observed_at: datetime) -> IdentityMapping:
        """Ancla `merged_into` -- nunca borra esta fila (data-model.md
        §IdentityMapping invariante)."""
        return replace(self, merged_into=surviving_digest, observed_at=observed_at)


def require_hashed_digest(digest: str) -> None:
    """400 `IDENTITY_NOT_HASHED` (contracts/crm-link.md §2): ultima barrera
    de esquema antes de que un dato personal crudo entre en una fila que no
    debe tenerlo."""
    if len(digest) != _SHA256_HEX_LENGTH or not _is_hex(digest):
        raise InvalidIdentityDigestError(
            f"identity_digest debe ser sha256 hexadecimal de {_SHA256_HEX_LENGTH} caracteres"
        )


def _is_hex(value: str) -> bool:
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
