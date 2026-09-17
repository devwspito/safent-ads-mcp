"""Dobles en memoria de los puertos de `rules`: misma semantica observable
que los repositorios SQL, sin base de datos."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime

from safent_ads.rules.application.ports import (
    EmergencyBrakeRepository,
    GuardrailRepository,
    RuleFiringRepository,
    RuleRepository,
)
from safent_ads.rules.domain.autonomy import AutonomyLevel
from safent_ads.rules.domain.emergency_brake import BrakeScope, EmergencyBrake
from safent_ads.rules.domain.guardrail import GuardrailPolicy
from safent_ads.rules.domain.rule import Rule
from safent_ads.rules.domain.rule_firing import RuleFiring
from safent_ads.rules.domain.stored_rule import StoredRule
from safent_ads.rules.infrastructure.errors import DuplicateRuleCodeError
from safent_ads.shared.ids import EntityRef, PlatformCode

__all__ = [
    "InMemoryEmergencyBrakeRepository",
    "InMemoryGuardrailRepository",
    "InMemoryRuleFiringRepository",
    "InMemoryRuleRepository",
]


class InMemoryRuleRepository(RuleRepository):
    """El catalogo entra por `sync_catalog`; la autonomia y el interruptor
    los conserva la base, nunca el fichero."""

    def __init__(self) -> None:
        self._by_code: dict[str, StoredRule] = {}

    async def sync_catalog(self, rules: Sequence[Rule]) -> int:
        for rule in rules:
            stored = self._by_code.get(rule.code)
            autonomy = stored.rule.autonomy_level if stored else AutonomyLevel.NOTIFY
            self._by_code[rule.code] = StoredRule(
                rule=replace(rule, autonomy_level=autonomy),
                is_enabled=stored.is_enabled if stored else False,
            )
        return len(rules)

    async def get_by_code(self, code: str) -> StoredRule | None:
        return self._by_code.get(code)

    async def list_enabled(
        self, *, platform: PlatformCode | None = None
    ) -> Sequence[StoredRule]:
        return [
            stored
            for stored in sorted(self._by_code.values(), key=lambda item: item.code)
            if stored.is_enabled and (platform is None or stored.rule.platform == platform)
        ]

    async def set_autonomy(self, *, code: str, level: AutonomyLevel, enabled: bool) -> None:
        stored = self._by_code[code]
        self._by_code[code] = StoredRule(
            rule=replace(stored.rule, autonomy_level=level), is_enabled=enabled
        )

    async def list_all(
        self, *, platform: PlatformCode | None = None, enabled: bool | None = None
    ) -> Sequence[StoredRule]:
        return [
            stored
            for stored in sorted(self._by_code.values(), key=lambda item: item.code)
            if (platform is None or stored.rule.platform == platform)
            and (enabled is None or stored.is_enabled == enabled)
        ]

    async def create(self, rule: Rule, *, enabled: bool) -> StoredRule:
        if rule.code in self._by_code:
            raise DuplicateRuleCodeError(f"{rule.code} ya existe en el catalogo")
        stored = StoredRule(rule=rule, is_enabled=enabled)
        self._by_code[rule.code] = stored
        return stored


class InMemoryGuardrailRepository(GuardrailRepository):
    def __init__(self) -> None:
        self._by_account: dict[str, GuardrailPolicy] = {}

    async def save_for_account(
        self, *, account_ref: str, policy: GuardrailPolicy, currency: str
    ) -> None:
        del currency
        self._by_account[account_ref] = policy

    async def find_for_account(self, *, account_ref: str) -> GuardrailPolicy | None:
        return self._by_account.get(account_ref)


class InMemoryEmergencyBrakeRepository(EmergencyBrakeRepository):
    def __init__(self) -> None:
        self._brakes: list[EmergencyBrake] = []

    async def engage(self, brake: EmergencyBrake) -> None:
        if await self.find_active(scope=brake.scope) is not None:
            raise ValueError(f"ya hay un freno activo en {brake.scope.kind}")
        self._brakes.append(brake)

    async def release(
        self, *, scope: BrakeScope, released_by: str, released_at: datetime
    ) -> None:
        del released_by
        for index, brake in enumerate(self._brakes):
            if brake.scope == scope and brake.is_active:
                self._brakes[index] = replace(brake, released_at=released_at)

    async def find_active(self, *, scope: BrakeScope) -> EmergencyBrake | None:
        active = [b for b in self._brakes if b.scope == scope and b.is_active]
        return active[-1] if active else None


class InMemoryRuleFiringRepository(RuleFiringRepository):
    def __init__(self) -> None:
        self._firings: list[RuleFiring] = []

    async def record(self, firing: RuleFiring) -> None:
        self._firings.append(firing)

    async def last_fired_at(
        self, *, rule_code: str, entity_ref: EntityRef
    ) -> datetime | None:
        matching = [f.fired_at for f in self._matching(rule_code, entity_ref)]
        return max(matching, default=None)

    async def count_on_day(
        self, *, rule_code: str, entity_ref: EntityRef, day: date
    ) -> int:
        return sum(
            1 for f in self._matching(rule_code, entity_ref) if f.fired_at.date() == day
        )

    def _matching(self, rule_code: str, entity_ref: EntityRef) -> list[RuleFiring]:
        return [
            f for f in self._firings if f.rule_code == rule_code and f.entity_ref == entity_ref
        ]
