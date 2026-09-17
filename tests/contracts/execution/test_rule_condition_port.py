"""Contrato de `RuleConditionPort`: identico para el doble en memoria y para
`SqlRuleConditionPort`."""

from __future__ import annotations

from tests.contracts.execution.conftest import RuleConditionFixture


async def test_a_rule_whose_signal_is_live_fires(conditions: RuleConditionFixture) -> None:
    rule_code, entity_ref = await conditions.given_firing_rule()
    assert await conditions.conditions.is_condition_live(rule_code, entity_ref)


async def test_a_rule_without_a_live_signal_does_not_fire(
    conditions: RuleConditionFixture,
) -> None:
    rule_code, entity_ref = await conditions.given_quiet_rule()
    assert not await conditions.conditions.is_condition_live(rule_code, entity_ref)


async def test_stale_data_never_authorises(conditions: RuleConditionFixture) -> None:
    """`STALE_DATA`: una regla no dispara sobre metricas que no se han
    refrescado, aunque la ultima senal dijera que si."""
    rule_code, entity_ref = await conditions.given_firing_rule_with_stale_data()
    assert not await conditions.conditions.is_condition_live(rule_code, entity_ref)


async def test_a_rule_the_owner_did_not_enable_never_authorises(
    conditions: RuleConditionFixture,
) -> None:
    """El interruptor es del propietario (D-A1): una regla apagada no
    autoriza nada, por muy viva que este su senal."""
    rule_code, entity_ref = await conditions.given_disabled_rule()
    assert not await conditions.conditions.is_condition_live(rule_code, entity_ref)


async def test_an_approval_level_rule_never_authorises_rule_authorization(
    conditions: RuleConditionFixture,
) -> None:
    """FR-11/FR-12, contracts/mcp-tools.md comprobacion 1: solo `AUTO`
    autoriza una `rule_authorization`. `APPROVAL` encendida y disparando
    exige `human_approval`, nunca este camino."""
    rule_code, entity_ref = await conditions.given_firing_rule_with_approval_autonomy()
    assert not await conditions.conditions.is_condition_live(rule_code, entity_ref)
