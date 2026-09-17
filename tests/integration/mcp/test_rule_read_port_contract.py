"""`RuleReadPort`: `FakeRuleReadPort` (`mcp/testing/fakes.py`) contra
`SqlRuleReadPort` (esta lane). `explain_rule`/IDOR de `list_guardrails` son
SQL-only: el fake ni simula el cruce de negocio en guardarrailes ni evalua
la ultima senal contra la condicion (mismo hueco que el resto de bancos de
contrato de esta lane cuando el doble no modela esa rama)."""

from __future__ import annotations

import itertools
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.contracts.sql_fixtures import account_external_id, campaign_ref, seed_entity

from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.mcp.infrastructure.sql_rule_read_port import SqlRuleReadPort
from safent_ads.mcp.testing.fakes import BUSINESS_A, FakeRuleReadPort
from safent_ads.rules.domain.emergency_brake import (
    BrakeMode,
    BrakeScope,
    BrakeScopeKind,
    EmergencyBrake,
)
from safent_ads.rules.infrastructure.sql_repositories import SqlEmergencyBrakeRepository

_NOW = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
_CONDITION_JSON = (
    '{"clauses":[{"metric":"cpl","comparator":"gte","window":"7D",'
    '"threshold_kind":"absolute","value":30.0}]}'
)

_INSERT_RULE = text("""
    INSERT INTO rules (code, scope, platform, entity_level, description, source_url,
                       condition, thresholds, data_window, action, magnitude_pct,
                       autonomy_level, cooldown_minutes, max_firings_per_day, is_enabled)
    VALUES (:code, 'global', 'google', 'campaign', 'regla de prueba', 'https://example.test',
            CAST(:condition AS jsonb), '{}'::jsonb, '7D', 'SELL', 20.0, 'NOTIFY', 60, 1, true)
""")

_INSERT_GUARDRAIL = text("""
    INSERT INTO guardrails (scope, platform_account_id, currency, daily_cap_minor,
                            monthly_cap_minor, budget_floor_minor, budget_ceiling_minor,
                            max_step_pct, max_changes_per_entity_per_day)
    SELECT 'platform_account', account.id, 'EUR', 10000, 100000, 0, 100000, 20.0, 5
      FROM platform_accounts AS account
     WHERE account.platform = :platform AND account.external_account_id = :external_account_id
""")


_rule_code_sequence = itertools.count(50)


def _rule_code() -> str:
    # X01/X02 son codigos reales del catalogo (`rules.yaml`, sembrado por
    # `seed_rule_catalog()` en la misma base compartida de sesion): X50 en
    # adelante nunca colisiona con el catalogo real. Contador, no aleatorio:
    # varias llamadas en el mismo fichero no deben poder chocar entre si.
    return f"X{next(_rule_code_sequence):02d}"


async def _cleanup_sql_rule(factory: async_sessionmaker[AsyncSession], code: str) -> None:
    # `rules.scope = 'global'` no tiene FK a ningun negocio: una fila sin
    # limpiar contamina el conteo del catalogo real para el resto de la
    # sesion de pytest (`tests/integration/migrations/test_rules_guardrails.
    # py::test_seed_is_idempotent` cuenta filas exactas).
    async with factory() as session:
        await session.execute(
            text("DELETE FROM rules WHERE code = :code AND scope = 'global'"), {"code": code}
        )
        await session.commit()


@dataclass(slots=True)
class RuleFixture:
    port: object
    business_id: str
    rule_id: str
    account_ref: str


@pytest.fixture(
    params=[
        pytest.param("fake", id="fake"),
        pytest.param("sql", id="sql", marks=pytest.mark.integration),
    ]
)
async def rule(request: pytest.FixtureRequest) -> AsyncIterator[RuleFixture]:
    if request.param == "fake":
        yield RuleFixture(FakeRuleReadPort(), BUSINESS_A, "M05", "google:act_fake")
        return

    factory: async_sessionmaker[AsyncSession] = request.getfixturevalue("mcp_session_factory")
    code = _rule_code()
    entity_ref = campaign_ref(f"rule{uuid.uuid4().hex[:8]}", platform_value="google")
    async with factory() as session:
        business_id = await seed_entity(session, entity_ref)
        await session.execute(_INSERT_RULE, {"code": code, "condition": _CONDITION_JSON})
        await session.execute(
            _INSERT_GUARDRAIL,
            {"platform": "google", "external_account_id": account_external_id(entity_ref)},
        )
        await session.commit()
    account_ref = f"google:{account_external_id(entity_ref)}"
    port = SqlRuleReadPort(factory)
    try:
        yield RuleFixture(port, str(business_id), code, account_ref)
    finally:
        await _cleanup_sql_rule(factory, code)


async def test_list_rules_includes_the_seeded_rule(rule: RuleFixture) -> None:
    rules = await rule.port.list_rules(rule.business_id, platform=None, enabled=None)

    assert any(item.rule_id == rule.rule_id for item in rules)


async def test_get_rule_returns_a_readable_condition(rule: RuleFixture) -> None:
    detail = await rule.port.get_rule(rule.business_id, rule.rule_id)

    assert detail.summary.rule_id == rule.rule_id
    assert detail.condition


async def test_list_guardrails_returns_the_seeded_policy(rule: RuleFixture) -> None:
    guardrails = await rule.port.list_guardrails(rule.business_id, rule.account_ref)

    assert len(guardrails) == 1
    assert guardrails[0].scope_ref == rule.account_ref
    assert guardrails[0].max_changes_per_entity_per_day > 0


async def test_kill_switch_is_disengaged_by_default(rule: RuleFixture) -> None:
    status = await rule.port.get_kill_switch_status(rule.business_id)

    assert status.engaged is False


@pytest.mark.integration
async def test_sql_get_rule_unknown_code_raises_not_found(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    port = SqlRuleReadPort(mcp_session_factory)

    with pytest.raises(EntityNotFoundError):
        await port.get_rule(str(uuid.uuid4()), "Z99")


@pytest.mark.integration
async def test_sql_list_guardrails_never_leaks_another_business_account(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    entity_ref = campaign_ref(f"iso{uuid.uuid4().hex[:8]}", platform_value="google")
    async with mcp_session_factory() as session:
        await seed_entity(session, entity_ref)
        await session.execute(
            _INSERT_GUARDRAIL,
            {"platform": "google", "external_account_id": account_external_id(entity_ref)},
        )
        await session.commit()
    account_ref = f"google:{account_external_id(entity_ref)}"
    port = SqlRuleReadPort(mcp_session_factory)

    guardrails = await port.list_guardrails(str(uuid.uuid4()), account_ref)

    assert guardrails == []


@pytest.mark.integration
async def test_sql_explain_rule_without_a_signal_never_fires(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    entity_ref = campaign_ref(f"exp{uuid.uuid4().hex[:8]}", platform_value="google")
    code = _rule_code()
    async with mcp_session_factory() as session:
        business_id = await seed_entity(session, entity_ref)
        await session.execute(_INSERT_RULE, {"code": code, "condition": _CONDITION_JSON})
        await session.commit()
    port = SqlRuleReadPort(mcp_session_factory)

    try:
        explanation = await port.explain_rule(
            str(business_id), code, entity_ref=str(entity_ref)
        )

        assert explanation.would_fire is False
    finally:
        await _cleanup_sql_rule(mcp_session_factory, code)


@pytest.mark.integration
async def test_sql_explain_rule_rejects_entity_from_another_business(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner_ref = campaign_ref(f"own{uuid.uuid4().hex[:8]}", platform_value="google")
    code = _rule_code()
    async with mcp_session_factory() as session:
        await seed_entity(session, owner_ref)
        await session.execute(_INSERT_RULE, {"code": code, "condition": _CONDITION_JSON})
        await session.commit()
    port = SqlRuleReadPort(mcp_session_factory)

    try:
        with pytest.raises(EntityNotFoundError):
            await port.explain_rule(str(uuid.uuid4()), code, entity_ref=str(owner_ref))
    finally:
        await _cleanup_sql_rule(mcp_session_factory, code)


@pytest.mark.integration
async def test_sql_kill_switch_reflects_an_engaged_global_brake(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Un freno global es UNICO en toda la base (indice parcial de 0007): se
    # libera en `finally` para no dejar el interruptor pulsado para el resto
    # de la sesion de pytest, que comparte el mismo contenedor de Postgres.
    global_scope = BrakeScope(kind=BrakeScopeKind.GLOBAL)
    entity_ref = campaign_ref(f"brk{uuid.uuid4().hex[:8]}", platform_value="google")
    async with mcp_session_factory() as session:
        business_id = await seed_entity(session, entity_ref)
        await SqlEmergencyBrakeRepository(session).engage(
            EmergencyBrake(
                scope=global_scope,
                mode=BrakeMode.ALL,
                reason="prueba de integracion",
                engaged_by="tester",
                engaged_at=_NOW,
            )
        )
        await session.commit()
    port = SqlRuleReadPort(mcp_session_factory)

    try:
        status = await port.get_kill_switch_status(str(business_id))

        assert status.engaged is True
        assert status.scope == "global"
    finally:
        async with mcp_session_factory() as session:
            await SqlEmergencyBrakeRepository(session).release(
                scope=global_scope, released_by="tester", released_at=_NOW
            )
            await session.commit()
