"""`CreateRule` (contracts/rest-api.md §Reglas y guardarraíles, `POST
/rules`): alta de un codigo nuevo en el catalogo global.

Construir `Rule` (dominio) revalida aqui, antes de tocar la base, el mismo
invariante "AUTO nunca sube gasto" que ya protege `PUT /rules/{id}`
(FR-11/FR-12, `rules.domain.rule.Rule.__post_init__`) -- una sola verdad,
nunca una comprobacion aparte en el borde HTTP."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from safent_ads.rules.application.ports import RuleRepository
from safent_ads.rules.domain.autonomy import ActionKind, AutonomyLevel
from safent_ads.rules.domain.condition import Condition
from safent_ads.rules.domain.rule import Rule
from safent_ads.rules.domain.stored_rule import StoredRule
from safent_ads.shared.ids import EntityLevel, PlatformCode

__all__ = ["CreateRule", "CreateRuleCommand"]


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateRuleCommand:
    code: str
    platform: PlatformCode | None
    entity_level: EntityLevel
    description: str
    condition: Condition
    action_kind: ActionKind
    magnitude_pct: float | None
    autonomy_level: AutonomyLevel
    cooldown: timedelta
    source_url: str
    enabled: bool


class CreateRule:
    def __init__(self, rules: RuleRepository) -> None:
        self._rules = rules

    async def execute(self, command: CreateRuleCommand) -> StoredRule:
        rule = Rule(
            code=command.code,
            platform=command.platform,
            entity_level=command.entity_level,
            description=command.description,
            condition=command.condition,
            action_kind=command.action_kind,
            magnitude_pct=command.magnitude_pct,
            autonomy_level=command.autonomy_level,
            cooldown=command.cooldown,
            source_url=command.source_url,
        )
        return await self._rules.create(rule, enabled=command.enabled)
