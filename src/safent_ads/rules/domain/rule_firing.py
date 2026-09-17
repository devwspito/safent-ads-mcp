"""`RuleFiring` (data-model.md §Rule, tabla `rule_firings`): el rastro de que
una regla se evaluo y con que desenlace.

Es lo que hace comprobables el `cooldown` y el `max_firings_per_day`: sin
registro de disparos, ambos limites serian una promesa en memoria."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from safent_ads.shared.ids import EntityRef


class FiringOutcome(StrEnum):
    PROPOSED = "proposed"
    AUTHORIZED = "authorized"
    SUPPRESSED_COOLDOWN = "suppressed_cooldown"
    SUPPRESSED_DAILY_CAP = "suppressed_daily_cap"
    SUPPRESSED_BRAKE = "suppressed_brake"
    SUPPRESSED_STALE = "suppressed_stale"
    SUPPRESSED_GUARDRAIL = "suppressed_guardrail"


@dataclass(frozen=True, kw_only=True, slots=True)
class RuleFiring:
    rule_code: str
    entity_ref: EntityRef
    outcome: FiringOutcome
    fired_at: datetime
