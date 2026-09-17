"""`GET`/`POST /rules`, `DELETE /rules/{id}`, `POST /rules/{id}/simulate`,
`GET /guardrails` (`rules.presentation.rest.build_rules_router`, rama
gap-accounts-rules) y `GET /platform-accounts`
(`accounts.presentation.connections_router.build_connections_router`) de
extremo a extremo contra Postgres real -- mismo patron de aislamiento que
`test_execution_rest_rules_and_batch.py`/`test_idor_sweep_new_routes.py`
(cookie de sesion real, `httpx.ASGITransport`, `Container` abre su propio
motor)."""

from __future__ import annotations

import hashlib
import itertools
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from tests.contracts.sql_fixtures import account_external_id, campaign_ref, seed_entity
from tests.unit.composition.factories import build_api_settings

from safent_ads.accounts.presentation.connections_router import build_connections_router
from safent_ads.composition.api import _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import AuthenticatedCaller, get_authenticated_caller
from safent_ads.rules.domain.autonomy import ActionKind, AutonomyLevel
from safent_ads.rules.domain.condition import Comparator, Condition, ConditionClause, ThresholdKind
from safent_ads.rules.domain.guardrail import GuardrailPolicy
from safent_ads.rules.domain.rule import Rule
from safent_ads.rules.domain.rule_firing import FiringOutcome, RuleFiring
from safent_ads.rules.infrastructure.sql_repositories import (
    SqlGuardrailRepository,
    SqlRuleFiringRepository,
    SqlRuleRepository,
)
from safent_ads.rules.presentation.rest import build_rules_router
from safent_ads.shared.ids import EntityLevel, EntityRef
from safent_ads.signals.domain.cause import Cause
from safent_ads.signals.domain.gate_verdict import GateName, GateVerdict
from safent_ads.signals.domain.signal import Signal, SignalKind
from safent_ads.signals.domain.value_objects import Evidence, MoneyAtStake, SignalStrength
from safent_ads.signals.domain.window_span import WindowSpan
from safent_ads.signals.infrastructure.sql_repositories import SqlSignalRepository

pytestmark = pytest.mark.integration

_RAW_TOKEN = "integration-test-rules-accounts-token"  # noqa: S105 - fixture, no secreto real

# `ads_isolated` es COMPARTIDA y no se deshace entre tests (docstring de
# `tests/conftest.py::isolated_database_url`): cada test que persiste un
# `code` necesita el suyo propio, nunca reusar una constante de modulo --
# de lo contrario un test posterior hereda el `is_enabled`/`autonomy_level`
# que dejo otro (contaminacion detectada escribiendo esta rama). Banda
# 50-89, libre de X01/X02 (catalogo real) y de X90-X93 (reservada por
# `test_execution_rest_rules_and_batch.py`/`test_idor_sweep_new_routes.py`).
_CODE_COUNTER = itertools.count(50)


def _unique_code() -> str:
    return f"X{next(_CODE_COUNTER):02d}"


class _Seeded:
    def __init__(
        self, *, business_id: uuid.UUID, entity_ref: EntityRef, owner_id: uuid.UUID
    ) -> None:
        self.business_id = business_id
        self.entity_ref = entity_ref
        self.owner_id = owner_id
        self.account_ref = f"{entity_ref.platform.value}:{account_external_id(entity_ref)}"


class _TwoBusinesses:
    def __init__(
        self,
        *,
        business_a: uuid.UUID,
        entity_a: EntityRef,
        business_b: uuid.UUID,
        entity_b: EntityRef,
        owner_id: uuid.UUID,
    ) -> None:
        self.business_a = business_a
        self.entity_a = entity_a
        self.account_a = f"{entity_a.platform.value}:{account_external_id(entity_a)}"
        self.business_b = business_b
        self.entity_b = entity_b
        self.account_b = f"{entity_b.platform.value}:{account_external_id(entity_b)}"
        self.owner_id = owner_id


async def _insert_owner_and_session(
    session: AsyncSession, *, owner_id: uuid.UUID, session_id: uuid.UUID, now: datetime
) -> None:
    await session.execute(
        text(
            "INSERT INTO owners (id, email, password_hash) "
            "VALUES (:id, :email, :password_hash)"
        ),
        {
            "id": str(owner_id),
            "email": f"owner-{owner_id.hex[:8]}@safent.example",
            "password_hash": "argon2id$fixture$not-a-real-hash",  # noqa: S106
        },
    )
    await session.execute(
        text(
            "INSERT INTO sessions (id, owner_id, token_hash, created_at, expires_at) "
            "VALUES (:id, :owner_id, :token_hash, :created_at, :expires_at)"
        ),
        {
            "id": str(session_id),
            "owner_id": str(owner_id),
            "token_hash": hashlib.sha256(_RAW_TOKEN.encode("utf-8")).hexdigest(),
            "created_at": now,
            "expires_at": now + timedelta(hours=1),
        },
    )


@pytest.fixture
async def seeded_business(isolated_database_url: str) -> AsyncIterator[_Seeded]:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    owner_id = uuid.uuid4()
    session_id = uuid.uuid4()
    now = datetime.now(UTC)
    entity_ref = campaign_ref(f"c-{uuid.uuid4().hex[:10]}-rulesacc")

    async with AsyncSession(engine, expire_on_commit=False) as session:
        business_id = await seed_entity(session, entity_ref)
        await _insert_owner_and_session(session, owner_id=owner_id, session_id=session_id, now=now)
        await session.commit()
    try:
        yield _Seeded(business_id=business_id, entity_ref=entity_ref, owner_id=owner_id)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM sessions WHERE id = :id"), {"id": str(session_id)}
            )
        await engine.dispose()


@pytest.fixture
async def two_businesses(isolated_database_url: str) -> AsyncIterator[_TwoBusinesses]:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    owner_id = uuid.uuid4()
    session_id = uuid.uuid4()
    now = datetime.now(UTC)
    entity_a = campaign_ref(f"a-{uuid.uuid4().hex[:10]}-rulesacc")
    entity_b = campaign_ref(f"b-{uuid.uuid4().hex[:10]}-rulesacc")

    async with AsyncSession(engine, expire_on_commit=False) as session:
        business_a = await seed_entity(session, entity_a)
        business_b = await seed_entity(session, entity_b)
        await _insert_owner_and_session(session, owner_id=owner_id, session_id=session_id, now=now)
        await session.commit()
    try:
        yield _TwoBusinesses(
            business_a=business_a,
            entity_a=entity_a,
            business_b=business_b,
            entity_b=entity_b,
            owner_id=owner_id,
        )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM sessions WHERE id = :id"), {"id": str(session_id)}
            )
        await engine.dispose()


def _rules_client(container: Container) -> httpx.AsyncClient:
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.include_router(build_rules_router(container))
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://test", cookies={SESSION_COOKIE_NAME: _RAW_TOKEN}
    )


def _rules_client_scoped_to_business_a(
    container: Container, business_a: uuid.UUID
) -> httpx.AsyncClient:
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.include_router(build_rules_router(container))

    async def _restricted() -> AuthenticatedCaller:
        return AuthenticatedCaller(allowed_business_ids=frozenset({str(business_a)}))

    app.dependency_overrides[get_authenticated_caller] = _restricted
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://test", cookies={SESSION_COOKIE_NAME: _RAW_TOKEN}
    )


def _accounts_client(container: Container) -> httpx.AsyncClient:
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    # `build_connections_router` solo usa `settings` para OAuth
    # (broker/redirect_uri), nunca para la sesion de BD -- esa la da
    # `app.state.container`, ya abierto contra `isolated_database_url`.
    app.include_router(build_connections_router(build_api_settings()))
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://test", cookies={SESSION_COOKIE_NAME: _RAW_TOKEN}
    )


def _decrease_rule(code: str, *, autonomy: AutonomyLevel = AutonomyLevel.AUTO) -> Rule:
    return Rule(
        code=code,
        platform=None,
        entity_level=EntityLevel.CAMPAIGN,
        description="Regla de contrato: baja gasto",
        condition=Condition(
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
        action_kind=ActionKind.SELL,
        magnitude_pct=30.0,
        autonomy_level=autonomy,
        cooldown=timedelta(hours=6),
        source_url="https://example.test/rule",
    )


_RULE_RESPONSE_FIELDS = frozenset(
    {
        "rule_id",
        "code",
        "name",
        "scope",
        "platform",
        "condition_label",
        "window",
        "action_label",
        "magnitude_pct",
        "autonomy_level",
        "cooldown_hours",
        "is_enabled",
        "firings_30d",
        "hit_rate_pct",
        "increases_spend",
    }
)


# ---------------------------------------------------------------------------
# `GET /rules`
# ---------------------------------------------------------------------------


async def test_list_rules_returns_the_zod_shape(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    code = _unique_code()
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with container.session_factory() as session:
            await SqlRuleRepository(session).sync_catalog([_decrease_rule(code)])
            await SqlRuleRepository(session).set_autonomy(
                code=code, level=AutonomyLevel.AUTO, enabled=True
            )
            await session.commit()

        async with _rules_client(container) as client:
            response = await client.get(
                "/api/v1/rules", params={"business_id": str(seeded_business.business_id)}
            )
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        rule = next(item for item in items if item["code"] == code)
        assert set(rule.keys()) == _RULE_RESPONSE_FIELDS
        assert rule["rule_id"] == code
        assert rule["scope"] == "campaign"
        assert rule["autonomy_level"] == "AUTO"
        assert rule["is_enabled"] is True
        assert rule["increases_spend"] is False
        assert rule["window"] == "7D"
        assert rule["magnitude_pct"] == 30.0
        assert rule["cooldown_hours"] == 6.0
        assert rule["firings_30d"] == 0
        assert rule["hit_rate_pct"] is None
    finally:
        await container.aclose()


async def test_list_rules_filters_by_platform_and_enabled(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    code = _unique_code()
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with container.session_factory() as session:
            await SqlRuleRepository(session).sync_catalog([_decrease_rule(code)])
            await session.commit()

        async with _rules_client(container) as client:
            enabled_only = await client.get(
                "/api/v1/rules",
                params={"business_id": str(seeded_business.business_id), "enabled": "true"},
            )
            by_google = await client.get(
                "/api/v1/rules",
                params={"business_id": str(seeded_business.business_id), "platform": "google"},
            )
        assert code not in {item["code"] for item in enabled_only.json()["items"]}
        assert code not in {item["code"] for item in by_google.json()["items"]}
    finally:
        await container.aclose()


async def test_list_rules_reports_firings_and_hit_rate_for_the_business(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    code = _unique_code()
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with container.session_factory() as session:
            await SqlRuleRepository(session).sync_catalog([_decrease_rule(code)])
            firings = SqlRuleFiringRepository(session)
            now = datetime.now(UTC)
            await firings.record(
                RuleFiring(
                    rule_code=code,
                    entity_ref=seeded_business.entity_ref,
                    outcome=FiringOutcome.PROPOSED,
                    fired_at=now,
                )
            )
            await session.commit()

        async with _rules_client(container) as client:
            response = await client.get(
                "/api/v1/rules", params={"business_id": str(seeded_business.business_id)}
            )
        rule = next(item for item in response.json()["items"] if item["code"] == code)
        assert rule["firings_30d"] == 1
        assert rule["hit_rate_pct"] == 100.0
    finally:
        await container.aclose()


async def test_list_rules_requires_business_access(
    two_businesses: _TwoBusinesses, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with _rules_client_scoped_to_business_a(
            container, two_businesses.business_a
        ) as client:
            response = await client.get(
                "/api/v1/rules", params={"business_id": str(two_businesses.business_b)}
            )
        assert response.status_code == 404
    finally:
        await container.aclose()


# ---------------------------------------------------------------------------
# `POST /rules`
# ---------------------------------------------------------------------------


def _create_rule_body(*, code: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": code,
        "platform": "meta",
        "entity_level": "ad_set",
        "description": "Regla de contrato: alta via API",
        "condition": {
            "clauses": [
                {
                    "metric": "roas",
                    "comparator": "lt",
                    "window": "7d",
                    "threshold_kind": "absolute",
                    "value": 1.0,
                }
            ]
        },
        "action": "sell",
        "magnitude_pct": 25.0,
        "autonomy_level": "notify",
        "cooldown_hours": 12,
        "enabled": False,
    }
    body.update(overrides)
    return body


async def test_create_rule_persists_and_returns_the_zod_shape(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    code = _unique_code()
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with _rules_client(container) as client:
            response = await client.post(
                "/api/v1/rules",
                params={"business_id": str(seeded_business.business_id)},
                json=_create_rule_body(code=code),
            )
        assert response.status_code == 201, response.text
        body = response.json()
        assert set(body.keys()) == _RULE_RESPONSE_FIELDS
        assert body["code"] == code
        assert body["is_enabled"] is False
        assert body["autonomy_level"] == "NOTIFY"

        async with container.session_factory() as session:
            stored = await SqlRuleRepository(session).get_by_code(code)
        assert stored is not None
        assert stored.rule.action_kind is ActionKind.SELL
    finally:
        await container.aclose()


async def test_create_rule_rejects_auto_with_a_spend_increasing_action(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    code = _unique_code()
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        body = _create_rule_body(code=code, action="buy", autonomy_level="auto")
        async with _rules_client(container) as client:
            response = await client.post(
                "/api/v1/rules",
                params={"business_id": str(seeded_business.business_id)},
                json=body,
            )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "AUTO_WOULD_INCREASE_SPEND"

        async with container.session_factory() as session:
            assert await SqlRuleRepository(session).get_by_code(code) is None
    finally:
        await container.aclose()


async def test_create_rule_rejects_a_duplicate_code(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with _rules_client(container) as client:
            response = await client.post(
                "/api/v1/rules",
                params={"business_id": str(seeded_business.business_id)},
                json=_create_rule_body(code="M01"),
            )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "RULE_CODE_ALREADY_EXISTS"
    finally:
        await container.aclose()


async def test_create_rule_requires_business_access(
    two_businesses: _TwoBusinesses, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with _rules_client_scoped_to_business_a(
            container, two_businesses.business_a
        ) as client:
            response = await client.post(
                "/api/v1/rules",
                params={"business_id": str(two_businesses.business_b)},
                json=_create_rule_body(code=_unique_code()),
            )
        assert response.status_code == 404
    finally:
        await container.aclose()


# ---------------------------------------------------------------------------
# `DELETE /rules/{id}`
# ---------------------------------------------------------------------------


async def test_delete_rule_soft_deletes_and_logs_a_decision(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    code = _unique_code()
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with container.session_factory() as session:
            await SqlRuleRepository(session).sync_catalog([_decrease_rule(code)])
            await SqlRuleRepository(session).set_autonomy(
                code=code, level=AutonomyLevel.AUTO, enabled=True
            )
            await session.commit()

        async with _rules_client(container) as client:
            response = await client.delete(
                f"/api/v1/rules/{code}",
                params={"business_id": str(seeded_business.business_id)},
            )
        assert response.status_code == 204, response.text

        async with container.session_factory() as session:
            stored = await SqlRuleRepository(session).get_by_code(code)
            assert stored is not None
            assert stored.is_enabled is False
            # `autonomy_level` no se toca: solo el interruptor se apaga.
            assert stored.rule.autonomy_level is AutonomyLevel.AUTO
            log_row = (
                await session.execute(
                    text(
                        "SELECT payload::text AS payload FROM decision_log "
                        "WHERE business_id = :business_id AND event_type = 'rule_change' "
                        "ORDER BY seq DESC LIMIT 1"
                    ),
                    {"business_id": seeded_business.business_id},
                )
            ).one()
            assert "RuleDeleted" in log_row.payload
    finally:
        await container.aclose()


async def test_delete_rule_404_for_unknown_code(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with _rules_client(container) as client:
            response = await client.delete(
                "/api/v1/rules/Z99", params={"business_id": str(seeded_business.business_id)}
            )
        assert response.status_code == 404
    finally:
        await container.aclose()


# ---------------------------------------------------------------------------
# `POST /rules/{id}/simulate`
# ---------------------------------------------------------------------------


async def test_simulate_404_for_unknown_rule_code(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with _rules_client(container) as client:
            response = await client.post(
                "/api/v1/rules/Z99/simulate",
                params={"business_id": str(seeded_business.business_id)},
                json={},
            )
        assert response.status_code == 404
    finally:
        await container.aclose()


async def test_simulate_without_entity_ref_does_not_fire(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    code = _unique_code()
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with container.session_factory() as session:
            await SqlRuleRepository(session).sync_catalog([_decrease_rule(code)])
            await session.commit()

        async with _rules_client(container) as client:
            response = await client.post(
                f"/api/v1/rules/{code}/simulate",
                params={"business_id": str(seeded_business.business_id)},
                json={},
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["would_fire"] is False
        assert body["projected_diff"] is None
        assert body["reason"]
    finally:
        await container.aclose()


async def _seed_budget(session: AsyncSession, entity_ref: EntityRef, *, minor: int) -> None:
    await session.execute(
        text(
            "UPDATE ad_entities SET budget_amount_minor = :minor, budget_currency = 'EUR', "
            "budget_kind = 'daily' WHERE platform = :platform AND level = :level "
            "AND external_id = :external_id"
        ),
        {
            "minor": minor,
            "platform": entity_ref.platform.value,
            "level": entity_ref.level.value,
            "external_id": entity_ref.external_id,
        },
    )


def _matching_signal(entity_ref: EntityRef, *, rule_code: str) -> Signal:
    return Signal(
        entity_ref=entity_ref,
        kind=SignalKind.SELL,
        strength=SignalStrength(80),
        cause=Cause.ROAS_BELOW_TARGET_SUSTAINED,
        cause_sentence="ROAS 0.80 por debajo del objetivo 1.00 en 7 dias.",
        span=WindowSpan.D7,
        money_at_stake=MoneyAtStake(minor_units=10000, currency="EUR"),
        evidence=Evidence(metric="roas", actual=0.8, target=1.0, baseline=None, span=WindowSpan.D7),
        gate_verdicts=(GateVerdict.ok(GateName.LEARNING),),
        emitted_at=datetime.now(UTC),
        rule_code=rule_code,
    )


async def test_simulate_fires_and_projects_a_budget_diff(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    code = _unique_code()
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with container.session_factory() as session:
            await SqlRuleRepository(session).sync_catalog([_decrease_rule(code)])
            await _seed_budget(session, seeded_business.entity_ref, minor=10000)
            await SqlSignalRepository(session, cycle_id=uuid.uuid4()).save(
                _matching_signal(seeded_business.entity_ref, rule_code=code)
            )
            await session.commit()

        async with _rules_client(container) as client:
            response = await client.post(
                f"/api/v1/rules/{code}/simulate",
                params={"business_id": str(seeded_business.business_id)},
                json={"entity_ref": str(seeded_business.entity_ref)},
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["would_fire"] is True
        assert body["projected_diff"] == {
            "parametro": "daily_budget",
            "valor_actual": 100.0,
            "valor_propuesto": 70.0,
        }
    finally:
        await container.aclose()


async def test_simulate_404_when_entity_belongs_to_another_business(
    two_businesses: _TwoBusinesses, isolated_database_url: str
) -> None:
    code = _unique_code()
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with container.session_factory() as session:
            await SqlRuleRepository(session).sync_catalog([_decrease_rule(code)])
            await session.commit()

        async with _rules_client(container) as client:
            response = await client.post(
                f"/api/v1/rules/{code}/simulate",
                params={"business_id": str(two_businesses.business_a)},
                json={"entity_ref": str(two_businesses.entity_b)},
            )
        assert response.status_code == 404
    finally:
        await container.aclose()


async def test_simulate_never_writes_anything(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    code = _unique_code()
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with container.session_factory() as session:
            await SqlRuleRepository(session).sync_catalog([_decrease_rule(code)])
            await _seed_budget(session, seeded_business.entity_ref, minor=10000)
            await SqlSignalRepository(session, cycle_id=uuid.uuid4()).save(
                _matching_signal(seeded_business.entity_ref, rule_code=code)
            )
            await session.commit()

        async def _counts() -> tuple[int, int]:
            async with container.session_factory() as session:
                firings = (
                    await session.execute(text("SELECT count(*) FROM rule_firings"))
                ).scalar_one()
                proposals = (
                    await session.execute(text("SELECT count(*) FROM proposals"))
                ).scalar_one()
                return firings, proposals

        before = await _counts()
        async with _rules_client(container) as client:
            await client.post(
                f"/api/v1/rules/{code}/simulate",
                params={"business_id": str(seeded_business.business_id)},
                json={"entity_ref": str(seeded_business.entity_ref)},
            )
        after = await _counts()
        assert before == after
    finally:
        await container.aclose()


# ---------------------------------------------------------------------------
# `GET /guardrails?scope_ref`
# ---------------------------------------------------------------------------


async def _seed_guardrail(session: AsyncSession, *, account_ref: str) -> None:
    await SqlGuardrailRepository(session).save_for_account(
        account_ref=account_ref,
        policy=GuardrailPolicy(
            daily_cap_minor=40000,
            monthly_cap_minor=900000,
            floor_minor=6000,
            ceiling_minor=50000,
            max_step_pct=30.0,
            max_changes_per_day=2,
        ),
        currency="EUR",
    )


_GUARDRAIL_RESPONSE_FIELDS = frozenset(
    {
        "guardrail_id",
        "scope",
        "scope_label",
        "daily_cap",
        "monthly_cap",
        "budget_floor",
        "budget_ceiling",
        "max_step_pct",
        "max_changes_per_entity_per_day",
        "min_viable_spend",
        "currency",
    }
)


async def test_guardrails_lists_by_business_scope_ref(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with container.session_factory() as session:
            await _seed_guardrail(session, account_ref=seeded_business.account_ref)
            await session.commit()

        async with _rules_client(container) as client:
            response = await client.get(
                "/api/v1/guardrails", params={"scope_ref": str(seeded_business.business_id)}
            )
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert len(items) == 1
        assert set(items[0].keys()) == _GUARDRAIL_RESPONSE_FIELDS
        assert items[0]["guardrail_id"] == seeded_business.account_ref
        assert items[0]["scope"] == "platform_account"
        assert items[0]["daily_cap"] == 400.0
        assert items[0]["min_viable_spend"] == 0.0
    finally:
        await container.aclose()


async def test_guardrails_lists_by_account_scope_ref(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with container.session_factory() as session:
            await _seed_guardrail(session, account_ref=seeded_business.account_ref)
            await session.commit()

        async with _rules_client(container) as client:
            response = await client.get(
                "/api/v1/guardrails", params={"scope_ref": seeded_business.account_ref}
            )
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 1
    finally:
        await container.aclose()


async def test_guardrails_404_for_a_business_the_caller_cannot_see(
    two_businesses: _TwoBusinesses, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with _rules_client_scoped_to_business_a(
            container, two_businesses.business_a
        ) as client:
            response = await client.get(
                "/api/v1/guardrails", params={"scope_ref": str(two_businesses.business_b)}
            )
        assert response.status_code == 404
    finally:
        await container.aclose()


async def test_guardrails_404_for_an_account_of_another_business(
    two_businesses: _TwoBusinesses, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with container.session_factory() as session:
            await _seed_guardrail(session, account_ref=two_businesses.account_b)
            await session.commit()

        async with _rules_client_scoped_to_business_a(
            container, two_businesses.business_a
        ) as client:
            response = await client.get(
                "/api/v1/guardrails", params={"scope_ref": two_businesses.account_b}
            )
        assert response.status_code == 404
    finally:
        await container.aclose()


async def test_guardrails_422_for_an_invalid_scope_ref(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with _rules_client(container) as client:
            response = await client.get(
                "/api/v1/guardrails", params={"scope_ref": "not-a-valid-scope-ref"}
            )
        assert response.status_code == 422
    finally:
        await container.aclose()


# ---------------------------------------------------------------------------
# `GET /platform-accounts`
# ---------------------------------------------------------------------------

_PLATFORM_ACCOUNT_FIELDS = frozenset(
    {
        "platform_account_id",
        "platform",
        "external_account_id",
        "label",
        "status",
        "currency",
        "timezone",
        "api_tier",
        "token",
        "quota",
        "unavailable_levers",
        "last_synced_at",
        "last_error_code",
    }
)


async def test_list_platform_accounts_returns_the_zod_shape(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with _accounts_client(container) as client:
            response = await client.get(
                "/api/v1/platform-accounts",
                params={"business_id": str(seeded_business.business_id)},
            )
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert len(items) == 1
        assert set(items[0].keys()) == _PLATFORM_ACCOUNT_FIELDS
        assert items[0]["platform_account_id"] == seeded_business.account_ref
        assert set(items[0]["token"].keys()) == {"health", "expires_at", "checked_at"}
        assert items[0]["token"]["health"] == "ok"
    finally:
        await container.aclose()


async def test_list_platform_accounts_only_lists_its_own_business(
    two_businesses: _TwoBusinesses, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with _accounts_client(container) as client:
            response = await client.get(
                "/api/v1/platform-accounts",
                params={"business_id": str(two_businesses.business_a)},
            )
        items = response.json()["items"]
        assert [item["platform_account_id"] for item in items] == [two_businesses.account_a]
    finally:
        await container.aclose()


async def test_list_platform_accounts_404_for_a_nonexistent_business(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    """`seeded_business` solo aporta una sesion real valida (cookie ->
    owner): el `business_id` de la peticion es un UUID nuevo, de ningun
    negocio sembrado."""
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with _accounts_client(container) as client:
            response = await client.get(
                "/api/v1/platform-accounts", params={"business_id": str(uuid.uuid4())}
            )
        assert response.status_code == 404
    finally:
        await container.aclose()
