"""Banco de contrato de los puertos de `rules`: catalogo, guardarrailes,
freno de emergencia y disparos."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from safent_ads.rules.application.ports import (
    EmergencyBrakeRepository,
    GuardrailRepository,
    RuleFiringRepository,
    RuleRepository,
)
from safent_ads.rules.domain.guardrail import GuardrailPolicy
from safent_ads.rules.infrastructure.sql_repositories import (
    SqlEmergencyBrakeRepository,
    SqlGuardrailRepository,
    SqlRuleFiringRepository,
    SqlRuleRepository,
)
from safent_ads.rules.testing.in_memory_repositories import (
    InMemoryEmergencyBrakeRepository,
    InMemoryGuardrailRepository,
    InMemoryRuleFiringRepository,
    InMemoryRuleRepository,
)
from safent_ads.shared.ids import EntityRef
from tests.conftest import rolled_back_session
from tests.contracts.sql_fixtures import account_external_id, seed_entity

FIRED_AT = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)

POLICY = GuardrailPolicy(
    daily_cap_minor=40000,
    monthly_cap_minor=900000,
    floor_minor=6000,
    ceiling_minor=50000,
    max_step_pct=30.0,
    max_changes_per_day=2,
)


@dataclass(slots=True)
class RulesFixture:
    rules: RuleRepository
    guardrails: GuardrailRepository
    brakes: EmergencyBrakeRepository
    firings: RuleFiringRepository
    session: Any

    async def given_entity(self, entity_ref: EntityRef) -> str:
        """Devuelve la referencia de cuenta (`<plataforma>:<id externo>`) de
        la entidad. En memoria basta con componerla."""
        if self.session is not None:
            await seed_entity(self.session, entity_ref)
        return f"{entity_ref.platform.value}:{account_external_id(entity_ref)}"


@pytest.fixture(
    params=[
        pytest.param("in_memory", id="in_memory"),
        pytest.param("sql", id="sql", marks=pytest.mark.integration),
    ]
)
async def rules(request: pytest.FixtureRequest) -> AsyncIterator[RulesFixture]:
    if request.param == "in_memory":
        yield RulesFixture(
            rules=InMemoryRuleRepository(),
            guardrails=InMemoryGuardrailRepository(),
            brakes=InMemoryEmergencyBrakeRepository(),
            firings=InMemoryRuleFiringRepository(),
            session=None,
        )
        return
    database_url: str = request.getfixturevalue("database_url")
    async with rolled_back_session(database_url) as session:
        yield RulesFixture(
            rules=SqlRuleRepository(session),
            guardrails=SqlGuardrailRepository(session),
            brakes=SqlEmergencyBrakeRepository(session),
            firings=SqlRuleFiringRepository(session),
            session=session,
        )
