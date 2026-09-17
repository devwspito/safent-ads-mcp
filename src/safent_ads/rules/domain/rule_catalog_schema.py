"""Esquema pydantic para validar `rules.yaml` (tasks.md T038: 'loader +
pydantic schema in rules/domain/'). Frontera de confianza: el fichero es
codigo versionado, no entrada de usuario en tiempo de ejecucion, pero se
valida igual antes de convertirlo en los `Rule` puros del dominio — el
`Rule` que consume el resto de `rules`/`signals` nunca ve pydantic
(`parse_catalog` es la unica puerta)."""

from __future__ import annotations

from datetime import timedelta

import yaml
from pydantic import BaseModel, ConfigDict, Field

from safent_ads.rules.domain.autonomy import ActionKind, AutonomyLevel
from safent_ads.rules.domain.condition import Comparator, Condition, ConditionClause, ThresholdKind
from safent_ads.rules.domain.rule import Rule
from safent_ads.shared.ids import EntityLevel, PlatformCode


class ConditionClauseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str = Field(min_length=1)
    comparator: Comparator
    window: str = Field(min_length=1)
    threshold_kind: ThresholdKind
    value: float


class RuleSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    platform: PlatformCode | None = None
    entity_level: EntityLevel
    description: str = Field(min_length=1)
    conditions: list[ConditionClauseSchema] = Field(min_length=1)
    action_kind: ActionKind
    magnitude_pct: float | None = None
    autonomy_level: AutonomyLevel
    cooldown_hours: float = Field(ge=0)
    source_url: str = Field(min_length=1)
    notes: str | None = None


def parse_catalog(raw_yaml: str) -> tuple[Rule, ...]:
    documents = yaml.safe_load(raw_yaml)
    schemas = [RuleSchema.model_validate(entry) for entry in documents["rules"]]
    return tuple(_to_domain(schema) for schema in schemas)


def _to_domain(schema: RuleSchema) -> Rule:
    condition = Condition(
        clauses=tuple(
            ConditionClause(
                metric=clause.metric,
                comparator=clause.comparator,
                window=clause.window,
                threshold_kind=clause.threshold_kind,
                value=clause.value,
            )
            for clause in schema.conditions
        )
    )
    return Rule(
        code=schema.code,
        platform=schema.platform,
        entity_level=schema.entity_level,
        description=schema.description,
        condition=condition,
        action_kind=schema.action_kind,
        magnitude_pct=schema.magnitude_pct,
        autonomy_level=schema.autonomy_level,
        cooldown=timedelta(hours=schema.cooldown_hours),
        source_url=schema.source_url,
    )
