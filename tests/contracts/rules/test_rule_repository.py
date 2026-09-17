"""Contrato de `RuleRepository`: el fichero manda en los umbrales, la base
manda en la autonomia."""

from __future__ import annotations

from datetime import timedelta

import pytest

from safent_ads.rules.domain.autonomy import ActionKind, AutonomyLevel
from safent_ads.rules.domain.condition import Comparator, Condition, ConditionClause, ThresholdKind
from safent_ads.rules.domain.rule import Rule
from safent_ads.rules.domain.rule_catalog_loader import load_default_catalog
from safent_ads.rules.infrastructure.errors import DuplicateRuleCodeError
from safent_ads.shared.ids import EntityLevel, PlatformCode
from tests.contracts.rules.conftest import RulesFixture

CATALOG = load_default_catalog()

# `[MGX][0-9]{2}` banda reservada a fixtures de contrato (mismo criterio
# que `tests/integration/composition/test_execution_rest_rules_and_batch.py`
# X90-X93): fuera de M01-M24/G01-G11/X01-X02, nunca choca con el catalogo
# real ni con otro banco que use la misma base `ads_isolated`.
_NEW_CODE = "X96"


def _new_rule(**overrides: object) -> Rule:
    defaults: dict[str, object] = {
        "code": _NEW_CODE,
        "platform": None,
        "entity_level": EntityLevel.CAMPAIGN,
        "description": "Regla de contrato: alta",
        "condition": Condition(
            clauses=(
                ConditionClause(
                    metric="roas_7d",
                    comparator=Comparator.LT,
                    window="7d",
                    threshold_kind=ThresholdKind.ABSOLUTE,
                    value=1.0,
                ),
            )
        ),
        "action_kind": ActionKind.SELL,
        "magnitude_pct": 15.0,
        "autonomy_level": AutonomyLevel.NOTIFY,
        "cooldown": timedelta(hours=6),
        "source_url": "https://example.test/rule",
    }
    defaults.update(overrides)
    return Rule(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def defensive_code() -> str:
    """Una regla que baja gasto: es la unica clase que puede pasar a AUTO
    (FR-11), asi que sirve para probar la promocion sin chocar con el
    invariante."""
    return next(
        rule.code
        for rule in CATALOG
        if rule.action_kind in {ActionKind.SELL, ActionKind.EXIT}
    )


async def test_sync_loads_the_catalog_but_leaves_autonomy_alone(rules: RulesFixture) -> None:
    synced = await rules.rules.sync_catalog(CATALOG)

    stored = await rules.rules.get_by_code(CATALOG[0].code)
    assert synced == len(CATALOG)
    assert stored is not None
    assert stored.rule.condition == CATALOG[0].condition
    assert stored.rule.action_kind == CATALOG[0].action_kind
    assert stored.rule.cooldown == CATALOG[0].cooldown
    assert stored.rule.autonomy_level is AutonomyLevel.NOTIFY
    assert stored.is_enabled is False


async def test_sync_is_idempotent(rules: RulesFixture) -> None:
    await rules.rules.sync_catalog(CATALOG)
    first = await rules.rules.get_by_code(CATALOG[0].code)

    await rules.rules.sync_catalog(CATALOG)

    assert await rules.rules.get_by_code(CATALOG[0].code) == first


async def test_owner_calibration_survives_a_resync(
    rules: RulesFixture, defensive_code: str
) -> None:
    await rules.rules.sync_catalog(CATALOG)
    await rules.rules.set_autonomy(code=defensive_code, level=AutonomyLevel.AUTO, enabled=True)

    await rules.rules.sync_catalog(CATALOG)

    stored = await rules.rules.get_by_code(defensive_code)
    assert stored is not None
    assert stored.rule.autonomy_level is AutonomyLevel.AUTO
    assert stored.is_enabled is True


async def test_unknown_code_is_none(rules: RulesFixture) -> None:
    await rules.rules.sync_catalog(CATALOG)

    assert await rules.rules.get_by_code("Z99") is None


async def test_a_seeded_but_uncalibrated_rule_is_none_not_a_crash(rules: RulesFixture) -> None:
    """Regresion: `seed_rule_catalog()` (migracion 0007) siembra los 37
    codigos con `condition = {}` antes de que `sync_catalog` los calibre
    desde `rules.yaml`. Sincronizar un catalogo vacio los deja a todos en
    ese estado a medias -- `get_by_code` no debe tumbarse con un `KeyError`
    sobre `condition["clauses"]`, tiene que denegar por defecto (`None`)."""
    await rules.rules.sync_catalog([])

    assert await rules.rules.get_by_code("M01") is None


async def test_list_enabled_only_returns_what_the_owner_turned_on(
    rules: RulesFixture, defensive_code: str
) -> None:
    await rules.rules.sync_catalog(CATALOG)
    assert await rules.rules.list_enabled() == []

    await rules.rules.set_autonomy(code=defensive_code, level=AutonomyLevel.NOTIFY, enabled=True)

    enabled = await rules.rules.list_enabled()
    assert [stored.code for stored in enabled] == [defensive_code]


async def test_list_enabled_filters_by_platform(
    rules: RulesFixture, defensive_code: str
) -> None:
    await rules.rules.sync_catalog(CATALOG)
    await rules.rules.set_autonomy(code=defensive_code, level=AutonomyLevel.NOTIFY, enabled=True)
    platform = next(rule.platform for rule in CATALOG if rule.code == defensive_code)
    other = PlatformCode.GOOGLE if platform is PlatformCode.META else PlatformCode.META

    assert [stored.code for stored in await rules.rules.list_enabled(platform=platform)] == [
        defensive_code
    ]
    assert await rules.rules.list_enabled(platform=other) == []


async def test_list_all_includes_disabled_rules_unlike_list_enabled(
    rules: RulesFixture,
) -> None:
    await rules.rules.sync_catalog(CATALOG)

    all_codes = {stored.code for stored in await rules.rules.list_all()}

    assert all_codes == {rule.code for rule in CATALOG}
    assert await rules.rules.list_enabled() == []


async def test_list_all_never_returns_uncalibrated_placeholder_rows(
    rules: RulesFixture,
) -> None:
    await rules.rules.sync_catalog([])

    assert await rules.rules.list_all() == []


async def test_list_all_filters_by_platform_and_by_enabled(
    rules: RulesFixture, defensive_code: str
) -> None:
    await rules.rules.sync_catalog(CATALOG)
    await rules.rules.set_autonomy(code=defensive_code, level=AutonomyLevel.NOTIFY, enabled=True)
    platform = next(rule.platform for rule in CATALOG if rule.code == defensive_code)
    other = PlatformCode.GOOGLE if platform is PlatformCode.META else PlatformCode.META

    by_platform = await rules.rules.list_all(platform=platform)
    by_other_platform = await rules.rules.list_all(platform=other)
    only_enabled = await rules.rules.list_all(enabled=True)
    only_disabled = await rules.rules.list_all(enabled=False)

    assert defensive_code in {stored.code for stored in by_platform}
    assert defensive_code not in {stored.code for stored in by_other_platform}
    assert {stored.code for stored in only_enabled} == {defensive_code}
    assert defensive_code not in {stored.code for stored in only_disabled}


async def test_create_persists_a_brand_new_code(rules: RulesFixture) -> None:
    stored = await rules.rules.create(_new_rule(), enabled=False)

    assert stored.code == _NEW_CODE
    assert stored.is_enabled is False
    fetched = await rules.rules.get_by_code(_NEW_CODE)
    assert fetched is not None
    assert fetched.rule.action_kind is ActionKind.SELL


async def test_create_can_enable_the_rule_immediately(rules: RulesFixture) -> None:
    stored = await rules.rules.create(_new_rule(code="X97"), enabled=True)

    assert stored.is_enabled is True
    fetched = await rules.rules.get_by_code("X97")
    assert fetched is not None
    assert fetched.is_enabled is True


async def test_create_rejects_a_code_that_already_exists(rules: RulesFixture) -> None:
    await rules.rules.create(_new_rule(code="X98"), enabled=False)

    with pytest.raises(DuplicateRuleCodeError):
        await rules.rules.create(_new_rule(code="X98"), enabled=False)
