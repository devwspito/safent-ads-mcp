"""US3 end to end (`quickstart.md §7`, tasks.md T074): approving without
fatigue, through the REAL REST router, the REAL Telegram approval state
machine (`ResolveCallback` + `ProposalApprovalGateway`, contracts/
telegram.md: "Telegram debe llamar al MISMO caso de uso que REST, nunca
uno paralelo"), and a real `ads-broker` for the paths that reach
`EXECUTED`.

`SubmitApproval.execute` (the single decision path both REST and Telegram
call into) used to share the `ExecutionAttempt.previous_value` omission
bug fixed in `test_us2_defensive_autonomy.py`'s first bank (now fixed
centrally in `ExecutionChokepoint._process`) -- every scenario below that
needs an `EXECUTED` outcome still uses a spend INCREASE (BUY) to keep this
bank about US3's own contract instead of re-proving that fix a third
time."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from safent_ads.composition.api import _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.composition.execution_rest import build_execution_router
from safent_ads.execution.domain.execution_attempt import ExecutionStatus
from safent_ads.execution.domain.guardrails import GuardrailScope, ScopeKind
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.notifications.application.resolve_callback import ResolveCallback
from safent_ads.notifications.domain.callback import CallbackAction, CallbackData, generate_nonce
from safent_ads.notifications.infrastructure.proposal_approval_gateway import (
    ProposalApprovalGateway,
)
from safent_ads.notifications.infrastructure.sql_repositories import SqlTelegramCallbackStore
from safent_ads.notifications.testing.fakes import FakeTelegramPairingGuard
from safent_ads.proposals.domain.cause import Cause, CauseKey
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Priority, Urgency
from safent_ads.proposals.domain.proposal import Proposal, ProposedDiff, new_proposal_id
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityRef
from tests.contracts.execution.conftest import GuardrailLimits, seed_guardrails
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.integration.composition.test_write_path_end_to_end import (
    _APPROVAL_SEED_B64,
    _campaign_row,
    _campaign_state_hash,
    _FakeGoogleSearchClient,
    _running_broker,
)
from tests.unit.composition.factories import build_api_settings

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
_RAW_TOKEN = "qa-us3-session-token"  # noqa: S105 - fixture, no secreto real
_CHAT_ID = 111222333
_MESSAGE_ID = 42
_GUARDRAILS = GuardrailLimits(
    daily_cap="500",
    monthly_cap="9000",
    floor="10",
    ceiling="300",
    max_step_pct=0.50,
    max_changes_per_entity_day=5,
)


class _Seeded:
    def __init__(
        self, *, business_id: uuid.UUID, entity_ref: EntityRef, owner_id: uuid.UUID
    ) -> None:
        self.business_id = business_id
        self.entity_ref = entity_ref
        self.owner_id = owner_id


@pytest.fixture
async def seeded_owner_session(isolated_database_url: str) -> AsyncIterator[_Seeded]:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    owner_id = uuid.uuid4()
    session_id = uuid.uuid4()
    now = datetime.now(UTC)
    entity_ref = campaign_ref(f"c-{uuid.uuid4().hex[:10]}-us3", platform_value="google")

    async with AsyncSession(engine, expire_on_commit=False) as session:
        business_id = await seed_entity(session, entity_ref)
        await session.execute(
            text(
                "INSERT INTO owners (id, email, password_hash) VALUES (:id, :email, :password_hash)"
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
                "token_hash": hashlib.sha256(_RAW_TOKEN.encode()).hexdigest(),
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


def _rest_client(container: Container) -> httpx.AsyncClient:
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.include_router(build_execution_router(container))
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://test", cookies={SESSION_COOKIE_NAME: _RAW_TOKEN}
    )


async def _seed_second_entity_in_business(
    session: AsyncSession, *, business_id: uuid.UUID, entity_ref: EntityRef
) -> None:
    """Misma forma que `tests.contracts.sql_fixtures.seed_entity`, pero
    cuelga la cuenta/entidad nueva de un negocio YA sembrado en vez de
    crear uno -- para que dos propuestas convivan en el mismo lote/negocio
    sin chocar con `ix_proposals_open_per_parameter`."""
    credential_id = uuid.uuid4()
    account_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:12]
    await session.execute(
        text("INSERT INTO credential_refs (id, platform, alias) VALUES (:id, :platform, :alias)"),
        {"id": credential_id, "platform": entity_ref.platform.value, "alias": f"alias-{suffix}"},
    )
    await session.execute(
        text(
            """
            INSERT INTO platform_accounts (id, business_id, platform, external_account_id,
                                           currency, timezone, api_tier, credential_ref_id,
                                           status)
            VALUES (:id, :business_id, :platform, :external_account_id, 'EUR',
                    'Europe/Madrid', 'meta_full', :credential_ref_id, 'ACTIVE')
            """
        ),
        {
            "id": account_id,
            "business_id": business_id,
            "platform": entity_ref.platform.value,
            "external_account_id": f"act_{entity_ref.external_id}",
            "credential_ref_id": credential_id,
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO ad_entities (business_id, platform_account_id, platform, level,
                                     external_id, name, status, platform_state_hash)
            VALUES (:business_id, :account_id, :platform, :level, :external_id,
                    'Campana adicional de contrato', 'ACTIVE', :state_hash)
            """
        ),
        {
            "business_id": business_id,
            "account_id": account_id,
            "platform": entity_ref.platform.value,
            "level": entity_ref.level.value,
            "external_id": entity_ref.external_id,
            "state_hash": "a" * 64,
        },
    )
    await session.flush()


def _raise_budget_proposal(
    business_id: uuid.UUID, entity_ref: EntityRef, *, expected_state_hash: str
) -> Proposal:
    diff = ProposedDiff.build(
        entity_ref=entity_ref,
        parameter="daily_budget",
        before=Money.of("70"),
        after=Money.of("90"),
    )
    return Proposal.raise_proposal(
        proposal_id=new_proposal_id(),
        business_id=BusinessId(business_id),
        diff=diff,
        classification=Classification.ROUTINE,
        cause=Cause(text="Presupuesto infrautilizado en 7D", rule_id=None),
        cause_key=CauseKey(entity_ref=entity_ref, rule_id="qa", cause_type="budget_underused"),
        evidence=(),
        estimated_impact=Money.of("120"),
        priority=Priority(urgency=Urgency.RECOMMENDED),
        now=_NOW,
        expires_at=_NOW + timedelta(hours=24),
        expected_state_hash=expected_state_hash,
    )


# ---------------------------------------------------------------------------
# quickstart §7 general: `POST /proposals/{id}/approve` (REST, panel) llega
# a `EXECUTED` a traves del bróker real -- el mismo `SubmitApproval` que
# invoca `ProposalApprovalGateway` para Telegram.
# ---------------------------------------------------------------------------


async def test_rest_approve_reaches_executed_through_the_real_broker(
    seeded_owner_session: _Seeded, isolated_database_url: str, tmp_path: Path
) -> None:
    # `seeded_owner_session` solo aporta la cookie de sesion valida (el
    # modelo de propietario unico autoriza cualquier negocio,
    # `AuthenticatedCaller.allowed_business_ids=None`): la entidad de este
    # banco se siembra aparte, con la forma exacta de resource_name que el
    # bróker/flagship esperan.
    customer_id = "9400000001"
    entity_ref = campaign_ref(f"customers/{customer_id}/campaigns/1", platform_value="google")
    row = _campaign_row(customer_id=customer_id, campaign_id="1", amount_micros=70_000_000)
    expected_hash = _campaign_state_hash(row)
    search_client = _FakeGoogleSearchClient(row)

    async with _running_broker(
        tmp_path, search_client=search_client, customer_id=customer_id
    ) as socket_path:
        settings = build_api_settings(
            database_url=isolated_database_url,
            broker_socket_path=str(socket_path),
            approval_signing_key=_APPROVAL_SEED_B64,
        )
        container = Container.build(settings)
        container.clock = FixedClock(_NOW)
        try:
            async with container.session_factory() as session:
                business_id = await seed_entity(session, entity_ref)
                await seed_guardrails(
                    session,
                    scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref)),
                    limits=_GUARDRAILS,
                    level="business",
                )
                proposal = _raise_budget_proposal(
                    business_id, entity_ref, expected_state_hash=expected_hash
                )
                await SqlProposalRepository(session).save(proposal)
                await session.commit()

            async with _rest_client(container) as client:
                response = await client.post(
                    f"/api/v1/proposals/{proposal.proposal_id}/approve",
                    json={"diff_hash": proposal.diff.diff_hash},
                )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["execution_id"]
            assert body["grace_seconds"] == 20

            # `SubmitApproval` agenda la ejecucion a `now + grace_seconds`
            # (T083, gracia servidora de 20 s para `routine`): con el
            # reloj fijo del contenedor, el chokepoint no la encuentra
            # reclamable hasta adelantarlo, igual que el siguiente tick
            # real de `ExecutionCycle` (30 s) lo haria en produccion.
            container.clock.advance_to(_NOW + timedelta(seconds=21))  # type: ignore[attr-defined]
            async with container.session_factory() as session:
                use_cases = container.build_execution_use_cases(session)
                outcome = await use_cases.chokepoint.run_once()
                await session.commit()
            assert outcome is ExecutionStatus.EXECUTED
            assert len(search_client.budget_mutations) == 1
        finally:
            await container.aclose()


# ---------------------------------------------------------------------------
# quickstart §7.1/§7.2 semantica de lote: un lote mixto (una propuesta que
# el guardarraíl bloquea de verdad junto a una que si se aprueba) produce
# `207` con desenlaces distintos por elemento -- distinto de
# `test_execution_rest_rules_and_batch.py::test_batch_approve_is_207_with_
# per_item_outcomes` (que solo prueba el 404 de "no encontrado"): aqui el
# fallo es una decision de NEGOCIO real (`GUARDRAIL_BLOCKED`), no un dato
# mal formado.
# ---------------------------------------------------------------------------


async def test_batch_approve_207_mixes_a_real_execution_with_a_real_guardrail_block(
    seeded_owner_session: _Seeded, isolated_database_url: str
) -> None:
    seeded = seeded_owner_session
    # `ix_proposals_open_per_parameter` (0008_proposals.py) admite como
    # maximo UNA propuesta abierta por entidad+parametro: la segunda
    # entidad del lote necesita su propia campana, no la del fixture --
    # sembrada DIRECTAMENTE en el negocio ya existente (no via
    # `seed_entity`, que siempre crea un negocio nuevo), para que
    # `/proposals/batch/approve` (business_id como query param) vea a las
    # dos propuestas.
    second_entity_ref = campaign_ref(
        f"c-{uuid.uuid4().hex[:10]}-us3-batch", platform_value="google"
    )
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    container.clock = FixedClock(_NOW)
    try:
        async with container.session_factory() as session:
            await _seed_second_entity_in_business(
                session, business_id=seeded.business_id, entity_ref=second_entity_ref
            )
            await seed_guardrails(
                session,
                scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(seeded.entity_ref)),
                # `daily_cap`, no el techo: `GuardrailEvaluator._clamp`
                # (execution/domain/guardrails.py) solo RECORTA suelo/techo/
                # salto (allowed=True con motivo) -- solo el tope diario/
                # mensual o el maximo de cambios por dia ponen allowed=False.
                # El salto maximo (100 % de 70 = 70) ya recorta el delta
                # ANTES de comprobar el tope, asi que este tiene que ser mas
                # estrecho que ese delta ya recortado (50 < 70), no que el
                # salto bruto pedido (430) -- si no, el "bloqueo" nunca se
                # dispara.
                limits=GuardrailLimits(
                    floor="10", ceiling="1000", max_step_pct=1.0, daily_cap="50"
                ),
                level="business",
            )
            allowed = Proposal.raise_proposal(
                proposal_id=new_proposal_id(),
                business_id=BusinessId(seeded.business_id),
                diff=ProposedDiff.build(
                    entity_ref=seeded.entity_ref,
                    parameter="daily_budget",
                    before=Money.of("70"),
                    after=Money.of("75"),
                ),
                classification=Classification.ROUTINE,
                cause=Cause(text="Contrato lote", rule_id=None),
                cause_key=CauseKey(
                    entity_ref=seeded.entity_ref, rule_id="qa", cause_type="allowed"
                ),
                evidence=(),
                estimated_impact=Money.of("10"),
                priority=Priority(urgency=Urgency.RECOMMENDED),
                now=_NOW,
                expires_at=_NOW + timedelta(hours=24),
                expected_state_hash="a" * 64,
            )
            blocked = Proposal.raise_proposal(
                proposal_id=new_proposal_id(),
                business_id=BusinessId(seeded.business_id),
                diff=ProposedDiff.build(
                    entity_ref=second_entity_ref,
                    parameter="daily_budget",
                    before=Money.of("70"),
                    after=Money.of("500"),
                ),
                classification=Classification.ROUTINE,
                cause=Cause(text="Contrato lote (fuera de techo)", rule_id=None),
                cause_key=CauseKey(
                    entity_ref=second_entity_ref, rule_id="qa", cause_type="blocked"
                ),
                evidence=(),
                estimated_impact=Money.of("430"),
                priority=Priority(urgency=Urgency.RECOMMENDED),
                now=_NOW,
                expires_at=_NOW + timedelta(hours=24),
                expected_state_hash="a" * 64,
            )
            proposals = SqlProposalRepository(session)
            await proposals.save(allowed)
            await proposals.save(blocked)
            await session.commit()
        async with _rest_client(container) as client:
            response = await client.post(
                "/api/v1/proposals/batch/approve",
                params={"business_id": str(seeded.business_id)},
                json={
                    "cause_key": "test",
                    "items": [
                        {
                            "proposal_id": str(allowed.proposal_id),
                            "diff_hash": allowed.diff.diff_hash,
                        },
                        {
                            "proposal_id": str(blocked.proposal_id),
                            "diff_hash": blocked.diff.diff_hash,
                        },
                    ],
                },
            )
        assert response.status_code == 207, response.text
        body = response.json()
        assert body["approved_count"] == 1
        assert body["failed_count"] == 1
        results_by_id = {result["proposal_id"]: result for result in body["results"]}
        assert results_by_id[str(allowed.proposal_id)]["ok"] is True
        blocked_result = results_by_id[str(blocked.proposal_id)]
        assert blocked_result["ok"] is False
        assert blocked_result["error_code"] == "GUARDRAIL_BLOCKED"
    finally:
        await container.aclose()


# ---------------------------------------------------------------------------
# contracts/telegram.md: Telegram y REST comparten `SubmitApproval` --
# aprobar la MISMA forma de propuesta por cada canal produce
# `Authorization` identicas salvo `channel`/`issued_by`.
# ---------------------------------------------------------------------------


async def test_telegram_and_rest_approvals_produce_the_same_shape_of_authorization(
    seeded_owner_session: _Seeded, isolated_database_url: str
) -> None:
    seeded = seeded_owner_session
    # Dos entidades: `ix_proposals_open_per_parameter` solo admite una
    # propuesta abierta por entidad+parametro a la vez.
    telegram_entity_ref = campaign_ref(
        f"c-{uuid.uuid4().hex[:10]}-us3-parity", platform_value="google"
    )
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    container.clock = FixedClock(_NOW)
    try:
        async with container.session_factory() as session:
            await _seed_second_entity_in_business(
                session, business_id=seeded.business_id, entity_ref=telegram_entity_ref
            )
            # Un unico guardarraíl de negocio cubre las dos entidades (ambas
            # cuelgan del mismo `business_id`); un segundo `seed_guardrails
            # (level="business")` violaria `guardrails_scope_unique`.
            await seed_guardrails(
                session,
                scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(seeded.entity_ref)),
                limits=_GUARDRAILS,
                level="business",
            )
            rest_proposal = Proposal.raise_proposal(
                proposal_id=new_proposal_id(),
                business_id=BusinessId(seeded.business_id),
                diff=ProposedDiff.build(
                    entity_ref=seeded.entity_ref,
                    parameter="daily_budget",
                    before=Money.of("70"),
                    after=Money.of("80"),
                ),
                classification=Classification.ROUTINE,
                cause=Cause(text="Contrato paridad REST", rule_id=None),
                cause_key=CauseKey(
                    entity_ref=seeded.entity_ref, rule_id="qa", cause_type="rest_parity"
                ),
                evidence=(),
                estimated_impact=Money.of("10"),
                priority=Priority(urgency=Urgency.RECOMMENDED),
                now=_NOW,
                expires_at=_NOW + timedelta(hours=24),
                expected_state_hash="a" * 64,
            )
            telegram_proposal = Proposal.raise_proposal(
                proposal_id=new_proposal_id(),
                business_id=BusinessId(seeded.business_id),
                diff=ProposedDiff.build(
                    entity_ref=telegram_entity_ref,
                    parameter="daily_budget",
                    before=Money.of("70"),
                    after=Money.of("80"),
                ),
                classification=Classification.ROUTINE,
                cause=Cause(text="Contrato paridad Telegram", rule_id=None),
                cause_key=CauseKey(
                    entity_ref=telegram_entity_ref, rule_id="qa", cause_type="telegram_parity"
                ),
                evidence=(),
                estimated_impact=Money.of("10"),
                priority=Priority(urgency=Urgency.RECOMMENDED),
                now=_NOW,
                expires_at=_NOW + timedelta(hours=24),
                expected_state_hash="a" * 64,
            )
            proposals = SqlProposalRepository(session)
            await proposals.save(rest_proposal)
            await proposals.save(telegram_proposal)
            await session.commit()

        async with _rest_client(container) as client:
            rest_response = await client.post(
                f"/api/v1/proposals/{rest_proposal.proposal_id}/approve",
                json={"diff_hash": rest_proposal.diff.diff_hash},
            )
        assert rest_response.status_code == 200, rest_response.text

        async with container.session_factory() as session:
            use_cases = container.build_execution_use_cases(session)
            gateway = ProposalApprovalGateway(
                session=session, use_cases=use_cases, clock=container.clock
            )
            telegram_result = await gateway.approve(
                proposal_id=str(telegram_proposal.proposal_id),
                diff_hash=telegram_proposal.diff.diff_hash,
                decided_by="telegram:987654321",
            )
            await session.commit()
        assert telegram_result.kind.value == "approved"

        async with container.session_factory() as session:
            rest_row = (
                (
                    await session.execute(
                        text(
                            "SELECT kind, decision, channel FROM approvals WHERE proposal_id = :id"
                        ),
                        {"id": str(rest_proposal.proposal_id)},
                    )
                )
                .mappings()
                .one()
            )
            telegram_row = (
                (
                    await session.execute(
                        text(
                            "SELECT kind, decision, channel FROM approvals WHERE proposal_id = :id"
                        ),
                        {"id": str(telegram_proposal.proposal_id)},
                    )
                )
                .mappings()
                .one()
            )

        assert rest_row["kind"] == telegram_row["kind"] == "human_approval"
        assert rest_row["decision"] == telegram_row["decision"] == "approved"
        assert rest_row["channel"] == "panel"
        assert telegram_row["channel"] == "telegram"
    finally:
        await container.aclose()


# ---------------------------------------------------------------------------
# contracts/telegram.md: subir gasto exige segundo toque; el primer `a`
# sobre una propuesta que sube gasto emite una tarjeta de confirmacion con
# un nonce NUEVO en vez de aprobar -- el segundo toque (`c`, el nonce
# rotado) es el que aprueba de verdad.
# ---------------------------------------------------------------------------


async def test_buy_proposal_requires_a_second_tap_with_a_rotated_nonce(
    seeded_owner_session: _Seeded, isolated_database_url: str
) -> None:
    seeded = seeded_owner_session
    # `telegram_callbacks.created_at` es `DEFAULT now()` (reloj REAL): el
    # reloj de este banco se ancla a `datetime.now(UTC)`, no al `_NOW` de
    # dominio, para que `callback_ttl`/el CHECK de la tabla sean
    # coherentes (mismo motivo que
    # tests/integration/notifications/test_sql_telegram_callback_store.py).
    real_now = datetime.now(UTC)
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    container.clock = FixedClock(real_now)
    try:
        async with container.session_factory() as session:
            await seed_guardrails(
                session,
                scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(seeded.entity_ref)),
                limits=_GUARDRAILS,
                level="business",
            )
            proposal = Proposal.raise_proposal(
                proposal_id=new_proposal_id(),
                business_id=BusinessId(seeded.business_id),
                diff=ProposedDiff.build(
                    entity_ref=seeded.entity_ref,
                    parameter="daily_budget",
                    before=Money.of("70"),
                    after=Money.of("90"),
                ),
                classification=Classification.ROUTINE,
                cause=Cause(text="Contrato segundo toque", rule_id=None),
                cause_key=CauseKey(
                    entity_ref=seeded.entity_ref, rule_id="qa", cause_type="second_tap"
                ),
                evidence=(),
                estimated_impact=Money.of("20"),
                priority=Priority(urgency=Urgency.RECOMMENDED),
                now=real_now,
                expires_at=real_now + timedelta(hours=24),
                expected_state_hash="a" * 64,
            )
            await SqlProposalRepository(session).save(proposal)
            await session.commit()

            store = SqlTelegramCallbackStore(session)
            approve_nonce = generate_nonce()
            await store.create(
                nonce=approve_nonce,
                proposal_id=str(proposal.proposal_id),
                chat_id=_CHAT_ID,
                message_id=_MESSAGE_ID,
                diff_hash=proposal.diff.diff_hash,
                action=CallbackAction.APPROVE,
                expires_at=real_now + timedelta(hours=6),
            )
            await session.commit()

            use_cases = container.build_execution_use_cases(session)
            gateway = ProposalApprovalGateway(
                session=session, use_cases=use_cases, clock=container.clock
            )
            resolver = ResolveCallback(
                store=store,
                gateway=gateway,
                clock=container.clock,
                pairing_guard=FakeTelegramPairingGuard(),
            )

            first_tap = await resolver.execute(
                callback_data=CallbackData.build(
                    proposal_id=str(proposal.proposal_id),
                    nonce=approve_nonce,
                    action=CallbackAction.APPROVE,
                ).encode(),
                chat_id=_CHAT_ID,
                from_user_id=987654321,
                message_id=_MESSAGE_ID,
            )
            assert first_tap.alert_text == "Confirma la subida"
            still_pending = (
                await session.execute(
                    text("SELECT state FROM proposals WHERE id = :id"),
                    {"id": str(proposal.proposal_id)},
                )
            ).scalar_one()
            assert still_pending == "pending", "el primer toque NUNCA aprueba una subida de gasto"

            # El nonce de aprobacion ya se consumio (de un solo uso); la
            # tarjeta de confirmacion trae uno NUEVO, distinto.
            confirm_nonce = _extract_confirm_nonce(first_tap.reply_markup)
            assert confirm_nonce != approve_nonce

            second_tap = await resolver.execute(
                callback_data=CallbackData.build(
                    proposal_id=str(proposal.proposal_id),
                    nonce=confirm_nonce,
                    action=CallbackAction.CONFIRM,
                ).encode(),
                chat_id=_CHAT_ID,
                from_user_id=987654321,
                message_id=_MESSAGE_ID,
            )
            assert second_tap.alert_text == "✅ Aprobado"
            await session.commit()

        async with container.session_factory() as session:
            approved_state = (
                await session.execute(
                    text("SELECT state FROM proposals WHERE id = :id"),
                    {"id": str(proposal.proposal_id)},
                )
            ).scalar_one()
        assert approved_state in ("approved", "scheduled")
    finally:
        await container.aclose()


def _extract_confirm_nonce(reply_markup: Any) -> str:
    for row in reply_markup:
        for button in row:
            if (
                button.callback_data.startswith("p:")
                and f":{CallbackAction.CONFIRM.value}" == (button.callback_data[-2:])
            ):
                return CallbackData.parse(button.callback_data).nonce
    raise AssertionError(f"no se encontro el boton de confirmar en {reply_markup!r}")


# ---------------------------------------------------------------------------
# contracts/telegram.md regla 1: un nonce caducado nunca aprueba -- llega
# "Caducada" y la tarjeta se reedita con el estado real, sin tocar la
# propuesta.
# ---------------------------------------------------------------------------


async def test_expired_nonce_is_rejected_never_approves(
    seeded_owner_session: _Seeded, isolated_database_url: str
) -> None:
    seeded = seeded_owner_session
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    container.clock = FixedClock(_NOW)
    try:
        async with container.session_factory() as session:
            proposal = Proposal.raise_proposal(
                proposal_id=new_proposal_id(),
                business_id=BusinessId(seeded.business_id),
                diff=ProposedDiff.build(
                    entity_ref=seeded.entity_ref,
                    parameter="daily_budget",
                    before=Money.of("70"),
                    after=Money.of("60"),
                ),
                classification=Classification.ROUTINE,
                cause=Cause(text="Contrato nonce caducado", rule_id=None),
                cause_key=CauseKey(
                    entity_ref=seeded.entity_ref, rule_id="qa", cause_type="expired_nonce"
                ),
                evidence=(),
                estimated_impact=Money.of("10"),
                priority=Priority(urgency=Urgency.RECOMMENDED),
                now=_NOW,
                expires_at=_NOW + timedelta(hours=24),
                expected_state_hash="a" * 64,
            )
            await SqlProposalRepository(session).save(proposal)
            await session.commit()

            store = SqlTelegramCallbackStore(session)
            expired_nonce = generate_nonce()
            reference = datetime.now(UTC)
            # `expires_at` relativo al reloj REAL: `telegram_callbacks.
            # created_at` es `DEFAULT now()` y el CHECK exige
            # `expires_at > created_at` (mismo motivo documentado en
            # tests/integration/notifications/test_sql_telegram_callback_store.py).
            await store.create(
                nonce=expired_nonce,
                proposal_id=str(proposal.proposal_id),
                chat_id=_CHAT_ID,
                message_id=_MESSAGE_ID,
                diff_hash=proposal.diff.diff_hash,
                action=CallbackAction.APPROVE,
                expires_at=reference + timedelta(seconds=1),
            )
            await session.commit()

            use_cases = container.build_execution_use_cases(session)
            gateway = ProposalApprovalGateway(
                session=session, use_cases=use_cases, clock=container.clock
            )
            resolver = ResolveCallback(
                store=store,
                gateway=gateway,
                clock=FixedClock(reference + timedelta(hours=1)),
                pairing_guard=FakeTelegramPairingGuard(),
            )

            outcome = await resolver.execute(
                callback_data=CallbackData.build(
                    proposal_id=str(proposal.proposal_id),
                    nonce=expired_nonce,
                    action=CallbackAction.APPROVE,
                ).encode(),
                chat_id=_CHAT_ID,
                from_user_id=987654321,
                message_id=_MESSAGE_ID,
            )
            assert outcome.alert_text == "Caducada"
            await session.commit()

        async with container.session_factory() as session:
            state = (
                await session.execute(
                    text("SELECT state FROM proposals WHERE id = :id"),
                    {"id": str(proposal.proposal_id)},
                )
            ).scalar_one()
        assert state == "pending", "un nonce caducado nunca debe aprobar la propuesta"
    finally:
        await container.aclose()
