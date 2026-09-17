"""`Business` (data-model.md): raiz de un negocio. Invariantes propias del
agregado; `slug` unico global se aplica en el repositorio (UNIQUE de BD), no
aqui — un agregado no puede validar unicidad sin consultar el mundo."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from safent_ads.shared.errors import DomainError
from safent_ads.shared.ids import BusinessId

_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


class InvalidBusinessError(DomainError):
    """`slug`, `timezone` o `reference_currency` con formato invalido."""


@dataclass(slots=True)
class Business:
    business_id: BusinessId
    name: str
    slug: str
    timezone: str
    reference_currency: str
    is_active: bool = field(default=True)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise InvalidBusinessError("name vacio")
        if not _SLUG_PATTERN.match(self.slug):
            raise InvalidBusinessError(f"slug invalido: {self.slug!r}")
        if not self.timezone.strip():
            raise InvalidBusinessError("timezone vacio")
        if not _CURRENCY_PATTERN.match(self.reference_currency):
            raise InvalidBusinessError(f"reference_currency invalida: {self.reference_currency!r}")

    def deactivate(self) -> None:
        self.is_active = False

    def activate(self) -> None:
        self.is_active = True
