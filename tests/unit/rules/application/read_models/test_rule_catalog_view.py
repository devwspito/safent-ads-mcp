"""`stored_rule_to_view` (`GET /rules`, `POST /rules`): mapeo puro
`StoredRule` -> `RuleView`, sin sesion -- la actividad de 30 dias (que si
necesita Postgres) vive en `list_rule_views` y se cubre en integracion."""

from __future__ import annotations

from datetime import timedelta

from safent_ads.rules.application.read_models.rule_catalog_view import stored_rule_to_view
from safent_ads.rules.domain.autonomy import ActionKind, AutonomyLevel
from safent_ads.rules.domain.condition import Comparator, Condition, ConditionClause, ThresholdKind
from safent_ads.rules.domain.rule import Rule
from safent_ads.rules.domain.stored_rule import StoredRule
from safent_ads.shared.ids import EntityLevel, PlatformCode


def _clause(**overrides: object) -> ConditionClause:
    defaults: dict[str, object] = {
        "metric": "roas",
        "comparator": Comparator.GT,
        "window": "7d",
        "threshold_kind": ThresholdKind.TARGET_RELATIVE_PCT,
        "value": 120,
    }
    defaults.update(overrides)
    return ConditionClause(**defaults)  # type: ignore[arg-type]


def _rule(**overrides: object) -> Rule:
    defaults: dict[str, object] = {
        "code": "M03",
        "platform": PlatformCode.META,
        "entity_level": EntityLevel.AD_SET,
        "description": "Bajar presupuesto por CPL sobre umbral",
        "condition": Condition(clauses=(_clause(),)),
        "action_kind": ActionKind.SELL,
        "magnitude_pct": 30.0,
        "autonomy_level": AutonomyLevel.AUTO,
        "cooldown": timedelta(hours=24),
        "source_url": "https://example.test/rule",
    }
    defaults.update(overrides)
    return Rule(**defaults)  # type: ignore[arg-type]


def test_maps_code_and_flat_fields() -> None:
    view = stored_rule_to_view(StoredRule(rule=_rule(), is_enabled=True))

    assert view.rule_id == "M03"
    assert view.code == "M03"
    assert view.name == "Bajar presupuesto por CPL sobre umbral"
    assert view.platform is PlatformCode.META
    assert view.magnitude_pct == 30.0
    assert view.autonomy_level is AutonomyLevel.AUTO
    assert view.cooldown_hours == 24.0
    assert view.is_enabled is True


def test_a_freshly_created_rule_has_no_activity_yet() -> None:
    view = stored_rule_to_view(StoredRule(rule=_rule(), is_enabled=False))

    assert view.firings_30d == 0
    assert view.hit_rate_pct is None


def test_missing_magnitude_defaults_to_zero_not_null() -> None:
    """`ruleSchema.magnitude_pct` (zod) no es opcional -- 24 de las 37
    reglas del catalogo no tienen magnitud (acciones binarias como pausar):
    discrepancia documentada en el informe de esta rama."""
    rule = _rule(
        action_kind=ActionKind.EXIT, magnitude_pct=None, autonomy_level=AutonomyLevel.NOTIFY
    )

    view = stored_rule_to_view(StoredRule(rule=rule, is_enabled=False))

    assert view.magnitude_pct == 0.0


def test_account_entity_level_scopes_to_platform_account() -> None:
    rule = _rule(
        entity_level=EntityLevel.ACCOUNT,
        action_kind=ActionKind.HOLD_ALL,
        autonomy_level=AutonomyLevel.NOTIFY,
    )

    view = stored_rule_to_view(StoredRule(rule=rule, is_enabled=False))

    assert view.scope == "platform_account"


def test_campaign_ad_set_ad_and_creative_levels_scope_to_campaign() -> None:
    for level in (EntityLevel.CAMPAIGN, EntityLevel.AD_SET, EntityLevel.AD, EntityLevel.CREATIVE):
        rule = _rule(entity_level=level)
        view = stored_rule_to_view(StoredRule(rule=rule, is_enabled=False))
        assert view.scope == "campaign", level


def test_increases_spend_reuses_the_domain_function() -> None:
    buy_rule = _rule(action_kind=ActionKind.BUY, autonomy_level=AutonomyLevel.APPROVAL)
    sell_rule = _rule(action_kind=ActionKind.SELL, autonomy_level=AutonomyLevel.AUTO)

    buy_view = stored_rule_to_view(StoredRule(rule=buy_rule, is_enabled=False))
    sell_view = stored_rule_to_view(StoredRule(rule=sell_rule, is_enabled=False))
    assert buy_view.increases_spend is True
    assert sell_view.increases_spend is False


def test_window_label_joins_multiple_clause_windows_without_duplicates() -> None:
    rule = _rule(
        condition=Condition(
            clauses=(
                _clause(window="3d"),
                _clause(window="7d"),
                _clause(window="7d"),
            )
        )
    )

    view = stored_rule_to_view(StoredRule(rule=rule, is_enabled=False))

    assert view.window == "3D/7D"


def test_condition_label_renders_one_clause_per_join() -> None:
    rule = _rule(
        condition=Condition(
            clauses=(
                _clause(metric="roas", comparator=Comparator.LT, window="3d", value=100),
                _clause(metric="roas", comparator=Comparator.LT, window="7d", value=100),
            )
        )
    )

    view = stored_rule_to_view(StoredRule(rule=rule, is_enabled=False))

    assert view.condition_label == "roas < 100% del objetivo (3D) y roas < 100% del objetivo (7D)"


def test_condition_label_renders_absolute_and_baseline_thresholds() -> None:
    absolute = _rule(
        condition=Condition(clauses=(_clause(threshold_kind=ThresholdKind.ABSOLUTE, value=1.5),))
    )
    baseline = _rule(
        condition=Condition(
            clauses=(_clause(threshold_kind=ThresholdKind.BASELINE_RELATIVE_PCT, value=200),)
        )
    )

    absolute_view = stored_rule_to_view(StoredRule(rule=absolute, is_enabled=False))
    baseline_view = stored_rule_to_view(StoredRule(rule=baseline, is_enabled=False))

    assert "1.5" in absolute_view.condition_label
    assert "línea base" in baseline_view.condition_label


def test_action_label_is_a_known_spanish_phrase_for_every_action_kind() -> None:
    for action in ActionKind:
        rule = _rule(action_kind=action, autonomy_level=AutonomyLevel.NOTIFY, magnitude_pct=None)
        view = stored_rule_to_view(StoredRule(rule=rule, is_enabled=False))
        assert view.action_label
