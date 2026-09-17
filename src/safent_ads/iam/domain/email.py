"""`Email` value object (data-model.md §Owner/Session): normaliza a
minusculas para que la unicidad de `owners.email` no dependa de mayusculas."""

from __future__ import annotations

import re
from dataclasses import dataclass

from safent_ads.shared.errors import DomainError

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InvalidEmailError(DomainError):
    """La cadena no tiene forma de correo electronico valido."""


@dataclass(frozen=True, slots=True)
class Email:
    value: str

    def __post_init__(self) -> None:
        normalized = self.value.strip().lower()
        if not _EMAIL_PATTERN.match(normalized):
            raise InvalidEmailError(f"correo invalido: {self.value!r}")
        object.__setattr__(self, "value", normalized)

    def __str__(self) -> str:
        return self.value
