"""`EmergencyBrake` (data-model.md §EmergencyBrake, tabla `emergency_brakes`;
spec.md FR-14: 'freno global y por cuenta, inmediato').

Invariantes: un freno activo por ambito como maximo (lo garantiza el indice
unico parcial del esquema, no la aplicacion) y un ambito coherente: el
global no nombra negocio ni cuenta."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from safent_ads.rules.domain.errors import InvalidBrakeScopeError
from safent_ads.shared.ids import BusinessId


class BrakeScopeKind(StrEnum):
    GLOBAL = "global"
    BUSINESS = "business"
    PLATFORM_ACCOUNT = "platform_account"


class BrakeMode(StrEnum):
    """`AUTONOMOUS` congela solo lo que actuaria sin preguntar; `ALL` congela
    tambien lo aprobado por una persona."""

    AUTONOMOUS = "autonomous"
    ALL = "all"


@dataclass(frozen=True, kw_only=True, slots=True)
class BrakeScope:
    kind: BrakeScopeKind
    business_id: BusinessId | None = None
    platform_account_ref: str | None = None

    def __post_init__(self) -> None:
        expected = {
            BrakeScopeKind.GLOBAL: (False, False),
            BrakeScopeKind.BUSINESS: (True, False),
            BrakeScopeKind.PLATFORM_ACCOUNT: (False, True),
        }[self.kind]
        if (self.business_id is not None, self.platform_account_ref is not None) != expected:
            raise InvalidBrakeScopeError(f"ambito {self.kind} mal formado")


@dataclass(frozen=True, kw_only=True, slots=True)
class EmergencyBrake:
    scope: BrakeScope
    mode: BrakeMode
    reason: str
    engaged_by: str
    engaged_at: datetime
    released_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.released_at is None
