"""Chaos bank (tasks.md T074: "caos: matar el worker a media ejecucion. ✓
ni cambio duplicado ni huérfano · C-8"): a REAL `ads-worker` (`python -m
safent_ads.orchestration --cycle execution --once`, the exact production
entrypoint) claims a pre-authorized `ExecutionAttempt` against real
Postgres, gets SIGKILLed while blocked mid-write on a REAL `ads-broker`
process (`_chaos_broker_process.py`, Google Ads SDK doubled per
contracts/platform-port.md), and a second worker run (simulating the
restart) has to finish the job without duplicating the platform mutation,
without leaving the claim orphaned forever, without double-notifying, and
with the decision-log chain still verifying.

Both the worker and the broker are separate OS processes on purpose:
`SqlExecutionQueue`'s `SKIP LOCKED`/lease-based reclaim (threat-model.md
C-8) and `WriteAuthorizationPipeline`'s idempotency-key replay only prove
anything about crash recovery if the crash is a REAL `SIGKILL` on a REAL
process holding its own DB connection/transaction -- an in-process
`asyncio.CancelledError` never tests the "connection died mid-transaction,
Postgres rolled it back" path this bank exists to cover.

Seeds the `ExecutionAttempt` directly (bypassing `SubmitApproval`/
`LiveRuleStep`) so this bank tests recovery from a crash, not the
`previous_value` fix already covered in `test_us2_defensive_autonomy.py`'s
first bank."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer

from safent_ads.audit.application.verify_decision_log_chain import VerifyDecisionLogChain
from safent_ads.audit.domain.chain import ChainVerifier
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.broker.domain.ledger_scope import LedgerScope
from safent_ads.broker.infrastructure.write_ledger_store import WriteLedgerStore
from safent_ads.composition.container import Container
from safent_ads.execution.application.chokepoint import (
    ExecutionChokepoint,  # noqa: F401 - documenta el objeto real que el subproceso ejecuta
)
from safent_ads.execution.domain.execution_attempt import ExecutionAttempt
from safent_ads.execution.domain.guardrails import GuardrailChange, GuardrailScope, ScopeKind
from safent_ads.execution.domain.identifiers import ExecutionId
from safent_ads.execution.infrastructure.sql_spend_ledger import SqlSpendLedger
from safent_ads.notifications.domain.value_objects import DedupeKey
from safent_ads.notifications.infrastructure.sql_repositories import SqlNotificationOutbox
from safent_ads.proposals.domain.authorization import (
    AuthorizationChannel,
    AuthorizationKind,
    sign_authorization,
)
from safent_ads.proposals.domain.cause import Cause, CauseKey
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.identifiers import AuthorizationId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Priority, Urgency
from safent_ads.proposals.domain.proposal import Proposal, ProposedDiff, new_proposal_id
from safent_ads.shared.clock import SystemClock
from safent_ads.shared.ids import BusinessId
from tests.conftest import _recreate_database, alembic_upgrade, to_alembic_dsn, with_database
from tests.contracts.execution.conftest import GuardrailLimits, seed_guardrails
from tests.contracts.sql_fixtures import seed_entity
from tests.integration.composition.test_write_path_end_to_end import (
    _APPROVAL_SEED_B64,
    _campaign_row,
    _campaign_state_hash,
)
from tests.unit.composition.factories import build_api_settings

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BROKER_SCRIPT = Path(__file__).resolve().parent / "_chaos_broker_process.py"
_CUSTOMER_ID = "9300000001"
_AMOUNT_MICROS = 100_000_000  # 100 EUR
_POLL_TIMEOUT_S = 15.0
_POLL_INTERVAL_S = 0.05


async def _poll_until(condition, *, timeout_s: float, description: str) -> None:
    """Espera deterministica por condicion (nunca `sleep` ciego): reintenta
    cada `_POLL_INTERVAL_S` hasta que `condition()` sea verdadera o expire
    `timeout_s`."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(_POLL_INTERVAL_S)
    raise AssertionError(f"tiempo agotado esperando: {description}")


def _worker_env(*, database_url: str, socket_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "ADS_DATABASE_URL": database_url,
            "ADS_SESSION_SECRET": "chaos-session-secret-0123456789abcde",
            "ADS_TOTP_ENC_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
            "ADS_MCP_TOKEN": "chaos-mcp-token",
            "ADS_APPROVAL_SIGNING_KEY": _APPROVAL_SEED_B64,
            "ADS_BROKER_SOCKET": str(socket_path),
            "ADS_PUBLIC_BASE_URL": "https://ads.chaos.test.ts.net",
            "TELEGRAM_BOT_TOKEN": "123456:chaos-bot-token",
            "TELEGRAM_OWNER_CHAT_IDS": "111222333",
        }
    )
    return env


async def _run_worker_once(env: dict[str, str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - argv fijo, sin entrada de usuario
        [sys.executable, "-m", "safent_ads.orchestration", "--cycle", "execution", "--once"],
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@pytest.fixture
def chaos_database_url(postgres_container: PostgresContainer) -> str:
    # The real worker claims globally: another test's unfinished attempt must
    # not consume its one cycle. Use a dedicated database, not a queue filter.
    name = f"ads_chaos_{uuid.uuid4().hex}"
    _recreate_database(postgres_container.get_connection_url(), name)
    url = with_database(to_alembic_dsn(postgres_container.get_connection_url()), name)
    alembic_upgrade(url)
    return url


@pytest.fixture
async def committing_session_factory(
    chaos_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(chaos_database_url, pool_pre_ping=True)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_worker_crash_mid_write_recovers_without_duplicating_the_change(  # noqa: PLR0915
    chaos_database_url: str,
    committing_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # Escenario de caos de un solo tramo (arrancar, matar, reiniciar,
    # verificar) -- dividirlo en sub-tests perderia la secuencia real que
    # el banco existe para probar.
    socket_path = tmp_path / "broker.sock"
    sentinel_path = tmp_path / "in_flight.marker"
    ledger_path = tmp_path / "write_ledger.sqlite3"
    credentials_dir = tmp_path / "credentials"

    row = _campaign_row(customer_id=_CUSTOMER_ID, campaign_id="1", amount_micros=_AMOUNT_MICROS)
    expected_hash = _campaign_state_hash(row)

    # ------------------------------------------------------------------
    # Fase 0: semilla REAL y COMMITEADA -- el worker (otro proceso) solo ve
    # lo que ya esta confirmado en Postgres, nunca una transaccion abierta
    # de este test.
    # ------------------------------------------------------------------
    async with committing_session_factory() as session:
        entity_ref, business_id, proposal, authorization = await _seed_authorized_attempt(
            session, customer_id=_CUSTOMER_ID, expected_hash=expected_hash
        )
        await session.commit()

    physical_account = LedgerScope(business_id, entity_ref.platform, _CUSTOMER_ID)
    broker_process = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            str(_BROKER_SCRIPT),
            str(socket_path),
            _CUSTOMER_ID,
            str(_AMOUNT_MICROS),
            str(sentinel_path),
            str(ledger_path),
            str(credentials_dir),
        ],
        cwd=_REPO_ROOT,
        env=dict(os.environ, QA_CHAOS_MUTATE_DELAY_SECONDS="3"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        await _poll_until(
            _broker_ready_poller(broker_process),
            timeout_s=_POLL_TIMEOUT_S,
            description="el broker de caos anuncie BROKER_READY",
        )

        env = _worker_env(database_url=chaos_database_url, socket_path=socket_path)

        # ------------------------------------------------------------------
        # Fase 1: primer worker -- reclama, empieza a escribir, se mata a
        # media escritura (el centinela confirma que el bróker ya esta
        # dentro de `mutate_campaign_budget`, no antes).
        # ------------------------------------------------------------------
        first_worker = await _run_worker_once(env)
        try:
            await _poll_until(
                sentinel_path.exists,
                timeout_s=_POLL_TIMEOUT_S,
                description="el centinela de escritura en vuelo",
            )
            first_worker.send_signal(signal.SIGKILL)
            first_worker.wait(timeout=_POLL_TIMEOUT_S)
        finally:
            if first_worker.poll() is None:
                first_worker.kill()
                first_worker.wait(timeout=_POLL_TIMEOUT_S)

        async with committing_session_factory() as session:
            claimed_row = (
                (
                    await session.execute(
                        text(
                            "SELECT outcome, started_at, attempt_count FROM executions "
                            "WHERE proposal_id = :id"
                        ),
                        {"id": str(proposal.proposal_id)},
                    )
                )
                .mappings()
                .one()
            )
            assert claimed_row["outcome"] == "RUNNING", (
                "el intento en vuelo queda RUNNING durable, nunca se fabrico un EXECUTED falso"
            )
            assert claimed_row["started_at"] is not None, "el reclamo quedo marcado (no NULL)"
            # `ExecutionAttempt.attempt_count` nace en 1 (execution/domain/
            # execution_attempt.py); `SqlExecutionQueue.claim_next` lo
            # incrementa UNA vez por reclamo -- 2 aqui es "reclamado
            # exactamente una vez", no dos.
            assert claimed_row["attempt_count"] == 2

            proposal_state = (
                await session.execute(
                    text("SELECT state FROM proposals WHERE id = :id"),
                    {"id": str(proposal.proposal_id)},
                )
            ).scalar_one()
            assert proposal_state == "executing", "la propuesta nunca se dio por ejecutada"
            assert (
                await session.execute(
                    text(
                        "SELECT count(*) FROM execution_reservations "
                        "WHERE business_id = :business AND state = 'ACTIVE'"
                    ),
                    {"business": business_id},
                )
            ).scalar_one() == 1

        # ------------------------------------------------------------------
        # Fase 2: el bróker (proceso vivo, ajeno al worker muerto) termina
        # su mutacion en vuelo por su cuenta -- se espera de forma
        # deterministica a que su propio libro de escrituras lo refleje.
        # ------------------------------------------------------------------
        ledger = WriteLedgerStore(ledger_path)
        today = SystemClock().now().date()
        await _poll_until(
            lambda: ledger.snapshot_today(physical_account, today).changes_count == 1,
            timeout_s=_POLL_TIMEOUT_S,
            description="el bróker anote la mutacion huerfana en su propio libro",
        )
        orphaned_snapshot = ledger.snapshot_today(physical_account, today)
        assert orphaned_snapshot.changes_count == 1

        # ------------------------------------------------------------------
        # Fase 3: "reinicio" -- pasado el arrendamiento de reclamo
        # (`DEFAULT_CLAIM_LEASE`, 5 min), un segundo worker reclama la
        # MISMA fila. Se retrasa `started_at` a mano: esperar 5 minutos
        # reales en un test seria un `sleep` ciego, justo lo que las
        # reglas duras prohiben.
        # ------------------------------------------------------------------
        async with committing_session_factory() as session:
            await session.execute(
                text(
                    "UPDATE executions SET started_at = now() - interval '10 minutes' "
                    "WHERE proposal_id = :id"
                ),
                {"id": str(proposal.proposal_id)},
            )
            await session.commit()

        second_worker = await _run_worker_once(env)
        stdout, stderr = second_worker.communicate(timeout=_POLL_TIMEOUT_S)
        assert second_worker.returncode == 0, (
            f"el segundo worker debe terminar limpio: stdout={stdout!r} stderr={stderr!r}"
        )

        async with committing_session_factory() as session:
            recovered_row = (
                (
                    await session.execute(
                        text(
                            "SELECT outcome, attempt_count FROM executions WHERE proposal_id = :id"
                        ),
                        {"id": str(proposal.proposal_id)},
                    )
                )
                .mappings()
                .one()
            )
            assert recovered_row["outcome"] == "SUCCEEDED", (
                "el reinicio debe terminar el trabajo huerfano"
            )
            assert recovered_row["attempt_count"] == 3, (
                "reclamado dos veces (el primer worker + el reinicio), mas el 1 inicial de la "
                "semilla"
            )

            proposal_state = (
                await session.execute(
                    text("SELECT state FROM proposals WHERE id = :id"),
                    {"id": str(proposal.proposal_id)},
                )
            ).scalar_one()
            assert proposal_state == "executed"

            # ---------------------------------------------------------
            # Sin cambio duplicado: el libro del bróker (independiente
            # del de `ads-api`) sigue en 1, pese a que el intento se
            # proceso dos veces (una por worker) -- la repeticion de
            # el recibo original permite recuperar el desenlace sin
            # tocar el SDK otra vez ni renovar una autorizacion.
            # ---------------------------------------------------------
            final_snapshot = ledger.snapshot_today(physical_account, today)
            assert final_snapshot.changes_count == 1, "ningun cambio duplicado en el bróker"

            app_ledger_rows = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM spend_ledger WHERE entity_ref = :entity_ref "
                        "AND kind = 'applied_change'"
                    ),
                    {"entity_ref": str(entity_ref)},
                )
            ).scalar_one()
            assert app_ledger_rows == 1, (
                "el propio libro de safent-ads tampoco anota el cambio dos veces"
            )
            assert (
                await session.execute(
                    text("SELECT state FROM execution_reservations WHERE business_id = :business"),
                    {"business": business_id},
                )
            ).scalar_one() == "SETTLED"

            # ---------------------------------------------------------
            # Cadena de auditoria: sigue verificando tras el caos.
            # ---------------------------------------------------------
            report = await VerifyDecisionLogChain(
                SqlDecisionLogRepository(session), ChainVerifier(), SystemClock()
            ).execute()
            assert report.chain_ok is True

            # ---------------------------------------------------------
            # Sin notificacion duplicada: la MISMA `dedupe_key` que
            # `PublishAutoReceipt` (notifications/application/
            # publish_auto_receipt.py) deriva de
            # proposal_id+diff_hash+chat_id solo se reserva una vez,
            # aunque el recibo se intentase publicar tras cada uno de
            # los dos intentos de ejecucion.
            # ---------------------------------------------------------
            outbox = SqlNotificationOutbox(session)
            dedupe_key = DedupeKey(
                f"auto_receipt:{proposal.proposal_id}:{proposal.diff.diff_hash}:111222333"
            )
            import uuid  # noqa: PLC0415

            from safent_ads.notifications.domain.notification import Notification  # noqa: PLC0415
            from safent_ads.notifications.domain.value_objects import (  # noqa: PLC0415
                Channel,
                NotificationKind,
                Severity,
            )

            receipt = Notification(
                notification_id=uuid.uuid4(),
                business_id=BusinessId(business_id),
                channel=Channel.TELEGRAM,
                severity=Severity.INFO,
                kind=NotificationKind.AUTO_RECEIPT,
                dedupe_key=dedupe_key,
                body="Recibo automatico de contrato",
            )
            first_reservation = await outbox.try_reserve(receipt)
            assert first_reservation is True
            receipt.mark_failed(attempts=1)  # evita el bug de message_id NULL en SENT (ver US1)
            await outbox.save(receipt)
            second_reservation = await outbox.try_reserve(receipt)
            assert second_reservation is False, (
                "un segundo intento de recibo (p. ej. tras el reinicio) nunca debe re-notificar"
            )
    finally:
        broker_process.send_signal(signal.SIGKILL)
        broker_process.wait(timeout=_POLL_TIMEOUT_S)
        # Nada que borrar: `approvals`/`decision_log` son solo-anexables por
        # trigger (0002_audit_chain.py, 0008_proposals.py: "revocar =
        # anexar"), y `executions`/`proposals` cuelgan de `approvals` via
        # `ON DELETE RESTRICT` -- la cadena de auditoria de este banco es
        # permanente por diseno, igual que en produccion. `business_id`
        # nace de un UUID nuevo por corrida (`seed_entity`), asi que las
        # filas que quedan nunca chocan con las de otra ejecucion del test.
        del business_id


def _broker_ready_poller(process: subprocess.Popen[bytes]) -> Callable[[], bool]:
    """El broker imprime `BROKER_READY` una sola vez al arrancar: se lee en
    modo no bloqueante para no congelar el poll deterministico de
    `_poll_until` mientras el servidor todavia no acepto conexiones."""
    assert process.stdout is not None
    fd = process.stdout.fileno()
    os.set_blocking(fd, False)
    buffer = b""

    def _poll() -> bool:
        nonlocal buffer
        try:
            buffer += os.read(fd, 4096)
        except BlockingIOError:
            pass
        return b"BROKER_READY" in buffer

    return _poll


async def _seed_authorized_attempt(session: AsyncSession, *, customer_id: str, expected_hash: str):
    from tests.contracts.sql_fixtures import campaign_ref  # noqa: PLC0415

    entity_ref = campaign_ref(f"customers/{customer_id}/campaigns/1", platform_value="google")
    business_id = await seed_entity(session, entity_ref)
    await session.execute(
        text(
            "UPDATE ad_entities SET budget_amount_minor = 10000, budget_currency = 'EUR', "
            "budget_kind = 'daily' WHERE entity_ref = :entity_ref"
        ),
        {"entity_ref": str(entity_ref)},
    )
    scope = GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref))
    await seed_guardrails(
        session, scope=scope, limits=GuardrailLimits(floor="60", ceiling="300"), level="business"
    )

    settings = build_api_settings(approval_signing_key=_APPROVAL_SEED_B64)
    container = Container.build(settings)
    try:
        now = SystemClock().now()
        proposal = Proposal.raise_proposal(
            proposal_id=new_proposal_id(),
            business_id=BusinessId(business_id),
            diff=ProposedDiff.build(
                entity_ref=entity_ref,
                parameter="daily_budget",
                before=Money.of("100"),
                after=Money.of("90"),
            ),
            classification=Classification.ROUTINE,
            cause=Cause(text="Contrato de caos", rule_id=None),
            cause_key=CauseKey(entity_ref=entity_ref, rule_id="qa", cause_type="chaos"),
            evidence=(),
            estimated_impact=Money.of("10"),
            priority=Priority(urgency=Urgency.RECOMMENDED),
            now=now,
            expires_at=now + timedelta(hours=1),
            expected_state_hash=expected_hash,
        )
        use_cases = container.build_execution_use_cases(session)
        await use_cases.proposals.save(proposal)
        guardrails = await use_cases.guardrail_sets.get_effective(scope)
        spend_ledger = SqlSpendLedger(session, container.clock, use_cases.execution_queue)
        ledger_snapshot = await spend_ledger.snapshot(scope, entity_ref)
        verdict = use_cases.guardrail_evaluator.evaluate(
            GuardrailChange(
                scope=scope,
                entity_ref=entity_ref,
                authorization_kind=AuthorizationKind.HUMAN_APPROVAL,
                before=Money.of("100"),
                after=Money.of("90"),
            ),
            guardrails,
            ledger_snapshot,
        )
        authorization = sign_authorization(
            authorization_id=AuthorizationId.new(),
            proposal_id=proposal.proposal_id,
            kind=AuthorizationKind.HUMAN_APPROVAL,
            proposal_classification=proposal.classification,
            diff_hash=proposal.diff.diff_hash,
            guardrail_verdict_hash=verdict.verdict_hash,
            issued_by="owner-de-contrato",
            channel=AuthorizationChannel.PANEL,
            decided_at=now,
            expires_at=now + timedelta(hours=1),
            signer=container.approval_key_pair.signer,
        )
        await use_cases.authorizations.save(authorization)
        proposal.approve(proposal.diff.diff_hash, now)
        await use_cases.proposals.save(proposal)
        proposal.schedule_execution(0, now)
        await use_cases.proposals.save(proposal)
        await use_cases.execution_queue.save(
            ExecutionAttempt(
                execution_id=ExecutionId.new(),
                business_id=proposal.business_id,
                proposal_id=proposal.proposal_id,
                authorization_id=authorization.authorization_id,
                idempotency_key=f"exec-{proposal.proposal_id}-{proposal.diff.diff_hash[:12]}",
                previous_value=Money.of("100"),
                platform_state_hash_before=expected_hash,
            )
        )
        return entity_ref, business_id, proposal, authorization
    finally:
        await container.aclose()
