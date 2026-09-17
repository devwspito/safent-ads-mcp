"""`POST /proposals/batch/approve`, `PUT /rules/{id}`, `PUT
/guardrails/{id}` y la puerta de autonomia (`GET`/`POST
/rules/autonomy-gate*`) de extremo a extremo contra Postgres real -- mismo
patron de aislamiento que `test_execution_rest.py` (cookie de sesion real,
`httpx.ASGITransport`, `Container` abre su propio motor)."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pyotp
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from tests.contracts.execution.conftest import GuardrailLimits, seed_guardrails
from tests.contracts.sql_fixtures import account_external_id, campaign_ref, seed_entity
from tests.integration.iam.confirmation_helpers import confirmed_request
from tests.unit.composition.factories import build_api_settings

from safent_ads.composition.api import _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.composition.execution_rest import build_execution_router
from safent_ads.execution.domain.guardrails import GuardrailScope, ScopeKind
from safent_ads.iam.infrastructure.aesgcm_totp_cipher import (
    PURPOSE_TOTP_SECRET,
    AesGcmTotpCipher,
)
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.proposals.domain.cause import Cause, CauseKey
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Priority, Urgency
from safent_ads.proposals.domain.proposal import Proposal, ProposedDiff, new_proposal_id
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.rules.domain.autonomy import ActionKind, AutonomyLevel
from safent_ads.rules.domain.condition import Comparator, Condition, ConditionClause, ThresholdKind
from safent_ads.rules.domain.rule import Rule
from safent_ads.rules.infrastructure.sql_repositories import SqlRuleRepository
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef

pytestmark = pytest.mark.integration

_RAW_TOKEN = "integration-test-rules-batch-token"  # noqa: S105 - fixture, no secreto real
_TOTP_SECRET = pyotp.random_base32()
_VALID_32_BYTE_KEY_B64 = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


class _Seeded:
    def __init__(
        self, *, business_id: uuid.UUID, entity_ref: EntityRef, owner_id: uuid.UUID
    ) -> None:
        self.business_id = business_id
        self.entity_ref = entity_ref
        self.owner_id = owner_id
        self.account_ref = f"{entity_ref.platform.value}:{account_external_id(entity_ref)}"


# `rules.code` exige `^[MGX][0-9]{2}$` (0007_rules_guardrails): el catalogo
# real usa M01-M24/G01-G11/X01-X02, asi que X90-X99 es una banda reservada
# a fixtures de contrato sin riesgo de colisionar con una regla real.
_TEST_RULE_BUY = "X90"
_TEST_RULE_SELL_GATED = "X91"
_TEST_RULE_SELL_CONFIRMED = "X92"


def _decrease_rule(code: str, *, autonomy: AutonomyLevel = AutonomyLevel.NOTIFY) -> Rule:
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
        magnitude_pct=20.0,
        autonomy_level=autonomy,
        cooldown=timedelta(hours=6),
        source_url="https://example.test/rule",
    )


@pytest.fixture
async def seeded_business(isolated_database_url: str) -> AsyncIterator[_Seeded]:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    owner_id = uuid.uuid4()
    session_id = uuid.uuid4()
    now = datetime.now(UTC)
    entity_ref = campaign_ref(f"c-{uuid.uuid4().hex[:10]}-rules")
    encrypted_secret = AesGcmTotpCipher(_VALID_32_BYTE_KEY_B64).encrypt(
        _TOTP_SECRET, purpose=PURPOSE_TOTP_SECRET
    )

    async with AsyncSession(engine, expire_on_commit=False) as session:
        business_id = await seed_entity(session, entity_ref)
        await session.execute(
            text(
                "INSERT INTO owners (id, email, password_hash, totp_secret_encrypted, "
                "totp_confirmed_at) VALUES (:id, :email, :password_hash, :totp_secret, :now)"
            ),
            {
                "id": str(owner_id),
                "email": f"owner-{owner_id.hex[:8]}@safent.example",
                "password_hash": "argon2id$fixture$not-a-real-hash",  # noqa: S106
                "totp_secret": encrypted_secret,
                "now": now,
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
        await session.commit()
    try:
        yield _Seeded(business_id=business_id, entity_ref=entity_ref, owner_id=owner_id)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM sessions WHERE id = :id"), {"id": str(session_id)}
            )
        await engine.dispose()


def _client(container: Container) -> httpx.AsyncClient:
    app = FastAPI()
    app.state.container = container
    # Mismo `exception_handler` que `composition/api.py::harden_api` monta en
    # produccion (envuelve `ApiError.detail` en `{"error": {...}}` per
    # contracts/rest-api.md) -- sin el resto de `harden_api` (CSRF, limite de
    # tasa), que exigiria cabeceras que estos tests de enrutado no fijan.
    app.add_exception_handler(ApiError, _handle_api_error)
    app.include_router(build_execution_router(container))
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: _RAW_TOKEN, "ads_csrf": "test-csrf"},
        headers={"X-CSRF-Token": "test-csrf"},
    )


async def _seed_pending_proposal(
    container: Container, *, business_id: uuid.UUID, entity_ref: EntityRef
) -> tuple[str, str]:
    proposal_id = new_proposal_id()
    now = container.clock.now()
    async with container.session_factory() as session:
        await seed_guardrails(
            session,
            scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref)),
            limits=GuardrailLimits(),
            level="business",
        )
        diff = ProposedDiff.build(
            entity_ref=entity_ref,
            parameter="daily_budget",
            before=Money.of("100"),
            after=Money.of("70"),
        )
        proposal = Proposal.raise_proposal(
            proposal_id=proposal_id,
            business_id=BusinessId(business_id),
            diff=diff,
            classification=Classification.ROUTINE,
            cause=Cause(text="Contrato batch", rule_id=None),
            cause_key=CauseKey(entity_ref=entity_ref, rule_id="agent", cause_type="test"),
            evidence=(),
            estimated_impact=Money.of("30"),
            priority=Priority(urgency=Urgency.RECOMMENDED),
            now=now,
            expires_at=now + timedelta(hours=24),
            expected_state_hash="a" * 64,
        )
        await SqlProposalRepository(session).save(proposal)
        await session.commit()
    return str(proposal_id), diff.diff_hash


async def test_batch_approve_is_207_with_per_item_outcomes(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    settings = build_api_settings(
        database_url=isolated_database_url,
        broker_socket_path="/tmp/safent-ads-wiring-b-test/batch-approve.sock",
    )
    container = Container.build(settings)
    try:
        proposal_id, diff_hash = await _seed_pending_proposal(
            container,
            business_id=seeded_business.business_id,
            entity_ref=seeded_business.entity_ref,
        )
        async with _client(container) as client:
            response = await client.post(
                "/api/v1/proposals/batch/approve",
                params={"business_id": str(seeded_business.business_id)},
                json={
                    "cause_key": "test",
                    "items": [
                        {"proposal_id": proposal_id, "diff_hash": diff_hash},
                        {"proposal_id": str(uuid.uuid4()), "diff_hash": "deadbeef"},
                    ],
                },
            )
        assert response.status_code == 207, response.text
        body = response.json()
        assert body["approved_count"] == 1
        assert body["failed_count"] == 1
        assert len(body["execution_ids"]) == 1
        results_by_ok = {result["ok"] for result in body["results"]}
        assert results_by_ok == {True, False}
        not_found = next(result for result in body["results"] if not result["ok"])
        assert not_found["error_code"] == "NOT_FOUND"
    finally:
        await container.aclose()


async def test_put_rule_auto_blocked_when_action_increases_spend(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        code = _TEST_RULE_BUY
        buy_rule = Rule(
            code=code,
            platform=None,
            entity_level=EntityLevel.CAMPAIGN,
            description="Regla de contrato: sube gasto",
            condition=Condition(
                clauses=(
                    ConditionClause(
                        metric="roas_7d",
                        comparator=Comparator.GT,
                        window="7d",
                        threshold_kind=ThresholdKind.ABSOLUTE,
                        value=3.0,
                    ),
                )
            ),
            action_kind=ActionKind.BUY,
            magnitude_pct=15.0,
            autonomy_level=AutonomyLevel.NOTIFY,
            cooldown=timedelta(hours=6),
            source_url="https://example.test/rule",
        )
        async with container.session_factory() as session:
            await SqlRuleRepository(session).sync_catalog([buy_rule])
            await session.commit()

        async with _client(container) as client:
            response = await client.put(
                f"/api/v1/rules/{code}",
                params={"business_id": str(seeded_business.business_id)},
                json={"autonomy_level": "auto"},
            )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "AUTO_WOULD_INCREASE_SPEND"
    finally:
        await container.aclose()


async def test_put_rule_auto_blocked_while_autonomy_gate_open(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        code = _TEST_RULE_SELL_GATED
        async with container.session_factory() as session:
            await SqlRuleRepository(session).sync_catalog([_decrease_rule(code)])
            await session.commit()

        async with _client(container) as client:
            response = await client.put(
                f"/api/v1/rules/{code}",
                params={"business_id": str(seeded_business.business_id)},
                json={"autonomy_level": "auto"},
            )
        assert response.status_code == 409, response.text
        body = response.json()
        assert body["error"]["code"] == "AUTONOMY_GATE_OPEN"
        assert body["error"]["details"]["missing"]
    finally:
        await container.aclose()


async def _confirm_all_gate_keys(client: httpx.AsyncClient, account_ref: str) -> None:
    """Each gate answer requires its own exact owner intent."""
    keys_values = (
        ("q2_autonomous_decrease", "true"),
        ("q3_monthly_cap", "sin_tope"),
        ("q8_browser_path", "false"),
    )
    for key, value in keys_values:
        response = await confirmed_request(
            client,
            "POST",
            "/api/v1/rules/autonomy-gate/confirmations",
            json={"platform_account_id": account_ref, "key": key, "value": value},
        )
        assert response.status_code == 200, response.text


async def test_autonomy_gate_ready_after_all_confirmations(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with _client(container) as client:
            before = await client.get(
                "/api/v1/rules/autonomy-gate",
                params={"business_id": str(seeded_business.business_id)},
            )
            assert before.status_code == 200, before.text
            assert before.json()["ready"] is False

            await _confirm_all_gate_keys(client, seeded_business.account_ref)

            after = await client.get(
                "/api/v1/rules/autonomy-gate",
                params={"business_id": str(seeded_business.business_id)},
            )
        assert after.status_code == 200
        assert after.json()["ready"] is True
    finally:
        await container.aclose()


async def test_autonomy_gate_confirmation_requires_explicit_intent(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with _client(container) as client:
            response = await client.post(
                "/api/v1/rules/autonomy-gate/confirmations",
                json={
                    "platform_account_id": seeded_business.account_ref,
                    "key": "q2_autonomous_decrease",
                    "value": "true",
                },
            )
        assert response.status_code == 428
        assert response.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
    finally:
        await container.aclose()


@pytest.mark.parametrize("change", ["replay", "different_key", "invalid"])
async def test_autonomy_gate_confirmation_proofs_are_one_shot_and_exact(
    seeded_business: _Seeded, isolated_database_url: str, change: str
) -> None:
    container = Container.build(build_api_settings(database_url=isolated_database_url))
    path = "/api/v1/rules/autonomy-gate/confirmations"
    body = {
        "platform_account_id": seeded_business.account_ref,
        "key": "q2_autonomous_decrease",
        "value": "true",
    }
    try:
        async with _client(container) as client:
            prepared = await client.post(path, json=body)
            assert prepared.status_code == 428
            proof = prepared.json()["error"]["details"]["confirmation_token"]
            if change == "replay":
                assert (
                    await client.post(path, json=body, headers={"X-Action-Confirmation": proof})
                ).status_code == 200
            if change == "different_key":
                body = {**body, "key": "q3_monthly_cap", "value": "sin_tope"}
            if change == "invalid":
                proof = "forged-proof"
            response = await client.post(path, json=body, headers={"X-Action-Confirmation": proof})
            assert response.status_code == 409
            assert response.json()["error"]["code"] == (
                "CONFIRMATION_USED" if change == "replay" else "CONFIRMATION_INVALID"
            )
    finally:
        await container.aclose()


async def test_put_rule_succeeds_once_gate_confirmed(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        code = _TEST_RULE_SELL_CONFIRMED
        async with container.session_factory() as session:
            await SqlRuleRepository(session).sync_catalog([_decrease_rule(code)])
            await session.commit()

        async with _client(container) as client:
            await _confirm_all_gate_keys(client, seeded_business.account_ref)
            response = await client.put(
                f"/api/v1/rules/{code}",
                params={"business_id": str(seeded_business.business_id)},
                # El panel envia el enum en mayusculas (`autonomyLevelSchema`).
                json={"autonomy_level": "AUTO"},
            )
        assert response.status_code == 200, response.text
        assert response.json()["autonomy_level"] == "AUTO"

        async with container.session_factory() as session:
            row = (
                await session.execute(
                    text("SELECT autonomy_level FROM rules WHERE code = :code"), {"code": code}
                )
            ).one()
            assert row.autonomy_level == "AUTO"
            log_row = (
                await session.execute(
                    text(
                        "SELECT event_type FROM decision_log WHERE business_id = :business_id "
                        "AND event_type = 'rule_change' ORDER BY seq DESC LIMIT 1"
                    ),
                    {"business_id": seeded_business.business_id},
                )
            ).one_or_none()
            assert log_row is not None
    finally:
        await container.aclose()


async def test_put_guardrail_rejects_scope_relaxation(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with container.session_factory() as session:
            await seed_guardrails(
                session,
                scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(seeded_business.entity_ref)),
                limits=GuardrailLimits(daily_cap="500"),
                level="business",
            )
            await session.commit()

        async with _client(container) as client:
            response = await client.put(
                f"/api/v1/guardrails/{seeded_business.account_ref}",
                params={"business_id": str(seeded_business.business_id)},
                json={
                    "daily_cap": "9000",
                    "monthly_cap": "10000",
                    "budget_floor": "10",
                    "budget_ceiling": "300",
                    "max_step_pct": 30,
                    "max_changes_per_entity_per_day": 2,
                },
            )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "SCOPE_CANNOT_RELAX"
    finally:
        await container.aclose()


async def test_put_guardrail_persists_a_tighter_policy(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with _client(container) as client:
            response = await client.put(
                f"/api/v1/guardrails/{seeded_business.account_ref}",
                params={"business_id": str(seeded_business.business_id)},
                json={
                    "daily_cap": "400",
                    "monthly_cap": "8000",
                    "budget_floor": "10",
                    "budget_ceiling": "250",
                    "max_step_pct": 20,
                    "max_changes_per_entity_per_day": 3,
                },
            )
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        assert body["daily_cap"]["amount"] == "400"
        assert body["max_changes_per_entity_per_day"] == 3
    finally:
        await container.aclose()


async def test_guardrail_route_returns_404_for_foreign_business(
    seeded_business: _Seeded, isolated_database_url: str
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with _client(container) as client:
            response = await client.put(
                f"/api/v1/guardrails/{seeded_business.account_ref}",
                params={"business_id": str(uuid.uuid4())},
                json={
                    "daily_cap": "400",
                    "monthly_cap": "8000",
                    "budget_floor": "10",
                    "budget_ceiling": "250",
                    "max_step_pct": 20,
                    "max_changes_per_entity_per_day": 3,
                },
            )
        assert response.status_code == 404
    finally:
        await container.aclose()
