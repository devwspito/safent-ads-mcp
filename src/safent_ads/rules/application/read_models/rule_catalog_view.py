"""Proyeccion de lectura de `GET /rules` (contracts/rest-api.md §Reglas y
guardarraíles): junta el catalogo calibrado (`RuleRepository.list_all`) con
el rendimiento de cada regla en los ultimos 30 dias para EL NEGOCIO que
pide la vista (`rule_firings`, via `ports.RuleActivityReadPort` -- I-1
revision final T130: la consulta SQL vive en `rules.infrastructure
.read_models.rule_catalog_view`, este modulo se queda con la proyeccion
pura).

`RuleView.rule_id == StoredRule.code`, sin prefijo cosmetico: el panel
manda `rule.rule_id` como `{id}` de `PUT /rules/{id}`
(`composition/execution_rest.py::put_rule`, ya cableado y probado), que
solo sabe buscar por `code`. Un prefijo (p. ej. `rule_M03`, como en los
fixtures de MSW del panel) romperia ese viaje de ida y vuelta -- ver el
informe de esta rama."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

from safent_ads.rules.application.ports import RuleRepository
from safent_ads.rules.domain.autonomy import ActionKind, AutonomyLevel, increases_spend
from safent_ads.rules.domain.condition import Comparator, ConditionClause, ThresholdKind
from safent_ads.rules.domain.rule import Rule
from safent_ads.rules.domain.stored_rule import StoredRule
from safent_ads.shared.ids import EntityLevel, PlatformCode

if TYPE_CHECKING:
    from safent_ads.rules.application.read_models.ports import RuleActivityReadPort

__all__ = ["RuleActivity", "RuleView", "list_rule_views", "stored_rule_to_view"]

_ACTIVITY_WINDOW: Final = timedelta(days=30)
_HIT_RATE_PRECISION: Final = 1
_PERCENT: Final = 100

_COMPARATOR_SYMBOL: Final[dict[Comparator, str]] = {
    Comparator.LT: "<",
    Comparator.LTE: "≤",
    Comparator.GT: ">",
    Comparator.GTE: "≥",
    Comparator.EQ: "=",
    Comparator.NE: "≠",
}

_ACTION_LABEL: Final[dict[ActionKind, str]] = {
    ActionKind.BUY: "Subir presupuesto",
    ActionKind.SELL: "Bajar presupuesto",
    ActionKind.EXIT: "Pausar entidad",
    ActionKind.UNPAUSE: "Reanudar entidad",
    ActionKind.HOLD_ALL: "Mantener sin cambios",
    ActionKind.NOTIFY_ONLY: "Solo avisar",
    ActionKind.ADD_NEGATIVE_KEYWORD: "Añadir palabra negativa",
    ActionKind.CREATIVE_KILL: "Retirar creativo",
    ActionKind.CREATIVE_SCALE: "Escalar creativo",
    ActionKind.TIGHTEN_TARGET: "Apretar objetivo",
    ActionKind.LOOSEN_TARGET: "Relajar objetivo",
    ActionKind.REPLACE_ASSET: "Sustituir activo",
}

# `EntityLevel.ACCOUNT` es el unico nivel de `rules.yaml` que no desciende de
# una campana: el resto (campaign/ad_set/ad/creative) vigilan algo DENTRO de
# una campana. `ruleScopeSchema` (panel) solo admite
# business/platform_account/campaign -- sin "account" ni "ad_set/ad/creative"
# -- asi que esta es la traduccion mas fiel sin inventar un cuarto valor.
_SCOPE_BY_ENTITY_LEVEL: Final[dict[EntityLevel, str]] = {
    EntityLevel.ACCOUNT: "platform_account",
    EntityLevel.CAMPAIGN: "campaign",
    EntityLevel.AD_SET: "campaign",
    EntityLevel.AD: "campaign",
    EntityLevel.CREATIVE: "campaign",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleView:
    rule_id: str
    code: str
    name: str
    scope: str
    platform: PlatformCode | None
    condition_label: str
    window: str
    action_label: str
    magnitude_pct: float
    autonomy_level: AutonomyLevel
    cooldown_hours: float
    is_enabled: bool
    firings_30d: int
    hit_rate_pct: float | None
    increases_spend: bool


@dataclass(frozen=True, slots=True)
class RuleActivity:
    """Disparos de una regla en los ultimos 30 dias para un negocio --
    publica: `ports.RuleActivityReadPort` (infraestructura) la construye a
    partir de `rule_firings`, esta proyeccion solo consume el resultado."""

    total: int
    successful: int

    @property
    def hit_rate_pct(self) -> float | None:
        if self.total == 0:
            return None
        return round(self.successful / self.total * _PERCENT, _HIT_RATE_PRECISION)


async def list_rule_views(
    activity: RuleActivityReadPort,
    rules: RuleRepository,
    *,
    business_id: str,
    platform: PlatformCode | None,
    enabled: bool | None,
    now: datetime,
) -> list[RuleView]:
    stored_rules = await rules.list_all(platform=platform, enabled=enabled)
    since = now - _ACTIVITY_WINDOW
    activity_by_code = await activity.activity_by_code(business_id=business_id, since=since)
    empty = RuleActivity(total=0, successful=0)
    return [_to_view(stored, activity_by_code.get(stored.code, empty)) for stored in stored_rules]


def stored_rule_to_view(stored: StoredRule) -> RuleView:
    """`RuleView` sin actividad (regla recien creada, `POST /rules`): 0
    disparos en 30d es un hecho, no un valor por defecto que oculte una
    consulta fallida."""
    return _to_view(stored, RuleActivity(total=0, successful=0))


def _to_view(stored: StoredRule, activity: RuleActivity) -> RuleView:
    rule = stored.rule
    return RuleView(
        rule_id=rule.code,
        code=rule.code,
        name=rule.description,
        scope=_SCOPE_BY_ENTITY_LEVEL[rule.entity_level],
        platform=rule.platform,
        condition_label=_condition_label(rule),
        window=_window_label(rule),
        action_label=_ACTION_LABEL[rule.action_kind],
        magnitude_pct=rule.magnitude_pct if rule.magnitude_pct is not None else 0.0,
        autonomy_level=rule.autonomy_level,
        cooldown_hours=rule.cooldown.total_seconds() / 3600,
        is_enabled=stored.is_enabled,
        firings_30d=activity.total,
        hit_rate_pct=activity.hit_rate_pct,
        increases_spend=increases_spend(rule.action_kind),
    )


def _window_label(rule: Rule) -> str:
    seen: dict[str, None] = {}
    for clause in rule.condition.clauses:
        seen.setdefault(clause.window.upper(), None)
    return "/".join(seen)


def _condition_label(rule: Rule) -> str:
    return " y ".join(_clause_label(clause) for clause in rule.condition.clauses)


def _clause_label(clause: ConditionClause) -> str:
    symbol = _COMPARATOR_SYMBOL[clause.comparator]
    threshold = _threshold_label(clause.threshold_kind, clause.value)
    return f"{clause.metric} {symbol} {threshold} ({clause.window.upper()})"


def _threshold_label(kind: ThresholdKind, value: float) -> str:
    if kind is ThresholdKind.TARGET_RELATIVE_PCT:
        return f"{value:g}% del objetivo"
    if kind is ThresholdKind.BASELINE_RELATIVE_PCT:
        return f"{value:g}% de la línea base"
    return f"{value:g}"
