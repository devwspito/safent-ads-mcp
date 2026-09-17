"""Puertos de `rules` (plan.md §5). Los repositorios devuelven agregados del
dominio, nunca filas.

`RuleRepository.sync_catalog` es la costura entre el fichero y la base: trae
umbrales y condiciones de `rules.yaml` y **no toca** el nivel de autonomia ni
el interruptor de habilitada, que son del propietario."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Protocol

from safent_ads.rules.domain.autonomy import AutonomyLevel
from safent_ads.rules.domain.emergency_brake import BrakeScope, EmergencyBrake
from safent_ads.rules.domain.guardrail import GuardrailPolicy
from safent_ads.rules.domain.rule import Rule
from safent_ads.rules.domain.rule_firing import RuleFiring
from safent_ads.rules.domain.stored_rule import StoredRule
from safent_ads.shared.ids import EntityRef, PlatformCode


class RuleRepository(Protocol):
    async def sync_catalog(self, rules: Sequence[Rule]) -> int: ...

    async def get_by_code(self, code: str) -> StoredRule | None: ...

    async def list_enabled(
        self, *, platform: PlatformCode | None = None
    ) -> Sequence[StoredRule]: ...

    async def list_all(
        self, *, platform: PlatformCode | None = None, enabled: bool | None = None
    ) -> Sequence[StoredRule]:
        """Catalogo calibrado completo (habilitado o no) -- `GET /rules`
        (panel): a diferencia de `list_enabled`, el propietario necesita ver
        tambien lo que sigue apagado para poder encenderlo."""
        ...

    async def set_autonomy(
        self, *, code: str, level: AutonomyLevel, enabled: bool
    ) -> None: ...

    async def create(self, rule: Rule, *, enabled: bool) -> StoredRule:
        """Alta de un `code` nuevo (`POST /rules`): nunca pisa una fila que
        ya exista -- ver `rules.infrastructure.errors.DuplicateRuleCodeError`."""
        ...


class GuardrailRepository(Protocol):
    """Un conjunto por ambito; aqui solo el ambito de cuenta, que es el que
    usa el evaluador (data-model.md §GuardrailSet)."""

    async def save_for_account(
        self, *, account_ref: str, policy: GuardrailPolicy, currency: str
    ) -> None: ...

    async def find_for_account(self, *, account_ref: str) -> GuardrailPolicy | None: ...


class EmergencyBrakeRepository(Protocol):
    async def engage(self, brake: EmergencyBrake) -> None: ...

    async def release(
        self, *, scope: BrakeScope, released_by: str, released_at: datetime
    ) -> None: ...

    async def find_active(self, *, scope: BrakeScope) -> EmergencyBrake | None: ...


class RuleFiringRepository(Protocol):
    async def record(self, firing: RuleFiring) -> None: ...

    async def last_fired_at(
        self, *, rule_code: str, entity_ref: EntityRef
    ) -> datetime | None: ...

    async def count_on_day(
        self, *, rule_code: str, entity_ref: EntityRef, day: date
    ) -> int: ...
