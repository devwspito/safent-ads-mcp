"""`rules.yaml`: carga las 37 reglas M01-M24/G01-G11/X01-X02, ninguna `AUTO`
sube gasto (tasks.md T038: `test_catalog_loads_37_rules`,
`test_no_spend_increasing_rule_is_auto`)."""

from __future__ import annotations

import pytest

from safent_ads.rules.domain.autonomy import AutonomyLevel, increases_spend
from safent_ads.rules.domain.rule_catalog_loader import load_default_catalog
from safent_ads.rules.domain.rule_catalog_schema import parse_catalog

_EXPECTED_CODES = (
    [f"M{n:02d}" for n in range(1, 25)] + [f"G{n:02d}" for n in range(1, 12)] + ["X01", "X02"]
)


@pytest.fixture(scope="module")
def catalog() -> tuple:
    return load_default_catalog()


def test_catalog_loads_37_rules(catalog: tuple) -> None:
    assert len(catalog) == 37


def test_catalog_codes_match_the_full_m_g_x_range(catalog: tuple) -> None:
    codes = {rule.code for rule in catalog}

    assert codes == set(_EXPECTED_CODES)


def test_catalog_codes_are_unique(catalog: tuple) -> None:
    codes = [rule.code for rule in catalog]

    assert len(codes) == len(set(codes))


def test_no_spend_increasing_rule_is_auto(catalog: tuple) -> None:
    violations = [
        rule.code
        for rule in catalog
        if rule.autonomy_level is AutonomyLevel.AUTO and increases_spend(rule.action_kind)
    ]

    assert violations == []


def test_auto_rules_are_defensive_only(catalog: tuple) -> None:
    auto_rules = [rule for rule in catalog if rule.autonomy_level is AutonomyLevel.AUTO]

    assert auto_rules  # hay al menos alguna regla autonoma
    assert all(not increases_spend(rule.action_kind) for rule in auto_rules)


def test_every_rule_has_a_source_url(catalog: tuple) -> None:
    assert all(rule.source_url for rule in catalog)


def test_rejects_a_catalog_with_an_unknown_field() -> None:
    raw = """
    rules:
      - code: X99
        platform: meta
        entity_level: ad_set
        description: regla de prueba
        conditions:
          - metric: roas
            comparator: gt
            window: 7d
            threshold_kind: absolute
            value: 1
        action_kind: sell
        autonomy_level: auto
        cooldown_hours: 1
        source_url: https://example.test
        unexpected_field: nope
    """

    with pytest.raises(Exception):  # noqa: B017, PT011 - pydantic.ValidationError
        parse_catalog(raw)


def test_rejects_an_auto_rule_whose_action_increases_spend() -> None:
    raw = """
    rules:
      - code: X99
        platform: meta
        entity_level: ad_set
        description: regla de prueba invalida
        conditions:
          - metric: roas
            comparator: gt
            window: 7d
            threshold_kind: absolute
            value: 1
        action_kind: buy
        autonomy_level: auto
        cooldown_hours: 1
        source_url: https://example.test
    """

    with pytest.raises(Exception):  # noqa: B017, PT011 - SpendIncreasingAutoRuleError
        parse_catalog(raw)
