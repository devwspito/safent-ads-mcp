"""`Rule` (data-model.md §Rule, tabla `rules`): catalogo M01-M24/G01-G11/X01-
X02 como datos, umbrales calibrables. Invariante: una `AUTO` nunca sube gasto
(FR-11/FR-12; `test_no_spend_increasing_rule_is_auto`)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from safent_ads.rules.domain.autonomy import ActionKind, AutonomyLevel, increases_spend
from safent_ads.rules.domain.condition import Condition
from safent_ads.rules.domain.errors import BlankRuleCodeError, InvalidCooldownError
from safent_ads.rules.domain.errors import SpendIncreasingAutoRuleError as SpendIncreasingAutoError
from safent_ads.shared.ids import EntityLevel, PlatformCode


@dataclass(frozen=True, kw_only=True, slots=True)
class Rule:
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

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise BlankRuleCodeError("code de regla vacio")
        if self.cooldown < timedelta(0):
            raise InvalidCooldownError(f"cooldown negativo en {self.code}: {self.cooldown}")
        if self.autonomy_level is AutonomyLevel.AUTO and increases_spend(self.action_kind):
            raise SpendIncreasingAutoError(
                f"{self.code}: AUTO no puede subir gasto (accion={self.action_kind.value})"
            )
