"""Contrato de `GuardrailSetRepository`: identico para el doble en memoria y
para `SqlGuardrailSetRepository`."""

from __future__ import annotations

import pytest

from safent_ads.proposals.domain.money import Money
from tests.contracts.execution.conftest import GuardrailFixture, GuardrailLimits


async def test_effective_set_carries_every_limit(guardrails: GuardrailFixture) -> None:
    scope = await guardrails.given_business_guardrails(
        GuardrailLimits(daily_cap="500", monthly_cap="10000", floor="10", ceiling="300")
    )

    effective = await guardrails.guardrails.get_effective(scope)

    assert effective.daily_cap == Money.of("500")
    assert effective.monthly_cap == Money.of("10000")
    assert effective.floor == Money.of("10")
    assert effective.ceiling == Money.of("300")
    assert effective.max_step_pct == pytest.approx(0.30)
    assert effective.max_changes_per_entity_day == 2


async def test_a_scope_without_guardrails_is_a_lookup_error(
    guardrails: GuardrailFixture,
) -> None:
    """Denegar por defecto (FR-13): sin limites no se ejecuta. Las dos
    implementaciones fallan con el mismo tipo de error, no una con `None`."""
    scope = await guardrails.given_entity_without_guardrails()

    with pytest.raises(LookupError):
        await guardrails.guardrails.get_effective(scope)
