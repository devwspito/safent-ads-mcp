"""`LiveMaintenanceStep`: adaptador real de `MaintenanceStepPort` (tasks.md
T078). Cada sub-paso reutiliza lo que YA existe en su propio contexto sin
modificarlo:

- `expire_stale_proposals`: `proposals.infrastructure.sql_proposal_repository.
  SqlProposalRepository.expire_due` -- escrito para este ciclo desde T057,
  nunca corrido periodicamente hasta ahora.
- `purge_telegram_artifacts`: SQL directo contra las tablas de
  `notifications` (otra lane, no se importa ese paquete -- mismo criterio
  que `economics_step.py::_active_offering_ids` sobre `offerings`, de
  `catalog`). `telegram_callbacks`/`telegram_brake_confirmations`: borrado
  de lo caducado (`ix_telegram_callbacks_expires` se doto para esto,
  0010_notifications). `telegram_owner_chats`: un codigo `pending` ya
  caducado vuelve a `unpaired` (mismo criterio de lectura que
  `notifications.domain.pairing.effective_status`, ahora persistido en vez
  de solo derivado al leer).
- `verify_decision_log_chain`: `audit.application.verify_decision_log_chain.
  VerifyDecisionLogChain`, sin cablear a ningun ciclo hasta ahora.
- `reconcile_platform_vs_crm`: `metrics.application.reconcile_platform_vs_crm.
  ReconcilePlatformVsCrm` (T116) sobre `reconciliation_adapters.py` (T078,
  cruza `crm`/`signals`/`optimization` sin que se importen entre si)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.audit.application.verify_decision_log_chain import VerifyDecisionLogChain
from safent_ads.audit.domain.chain import ChainVerifier
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.metrics.application.reconcile_platform_vs_crm import ReconcilePlatformVsCrm
from safent_ads.metrics.infrastructure.sql_repositories import SqlMetricFactRepository
from safent_ads.orchestration.infrastructure.reconciliation_adapters import (
    SqlCrmConversionsAdapter,
    SqlSignalContradictionRecorderAdapter,
)
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId
from safent_ads.signals.infrastructure.sql_repositories import SqlSignalReconciliationRepository

logger = structlog.get_logger(__name__)

_EXPIRE_BATCH_SIZE = 500
# T116: solo se reconcilian senales con evidencia lo bastante madura para
# que el CRM ya haya tenido tiempo de registrar la conversion (spec.md Edge
# Cases: "rezago de atribucion ... reevaluacion, nunca contradiccion
# silenciosa").
_RECONCILIATION_LAG = timedelta(days=3)

_DELETE_EXPIRED_TELEGRAM_CALLBACKS: Final = text(
    "DELETE FROM telegram_callbacks WHERE expires_at <= :now RETURNING nonce"
)
_DELETE_EXPIRED_BRAKE_CONFIRMATIONS: Final = text(
    "DELETE FROM telegram_brake_confirmations WHERE expires_at <= :now RETURNING nonce"
)
_EXPIRE_PENDING_PAIRING_CODES: Final = text("""
    UPDATE telegram_owner_chats
       SET status = 'unpaired', chat_id = NULL, pairing_code_hash = NULL,
           pairing_code_encrypted = NULL, code_expires_at = NULL, verified_at = NULL
     WHERE status = 'pending' AND code_expires_at <= :now
    RETURNING owner_id
""")


class LiveMaintenanceStep:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def expire_stale_proposals(self, now: datetime) -> None:
        async with self._session_factory() as session:
            expired = await SqlProposalRepository(session).expire_due(now, _EXPIRE_BATCH_SIZE)
            await session.commit()
        logger.info("maintenance_expired_proposals", count=len(expired))

    async def purge_telegram_artifacts(self, now: datetime) -> None:
        async with self._session_factory() as session:
            callbacks = await session.execute(_DELETE_EXPIRED_TELEGRAM_CALLBACKS, {"now": now})
            expired_callbacks = callbacks.all()
            confirmations = await session.execute(
                _DELETE_EXPIRED_BRAKE_CONFIRMATIONS, {"now": now}
            )
            expired_confirmations = confirmations.all()
            pairing_codes = await session.execute(_EXPIRE_PENDING_PAIRING_CODES, {"now": now})
            expired_pairing_codes = pairing_codes.all()
            await session.commit()
        logger.info(
            "maintenance_purged_telegram_artifacts",
            expired_callbacks=len(expired_callbacks),
            expired_brake_confirmations=len(expired_confirmations),
            expired_pairing_codes=len(expired_pairing_codes),
        )

    async def verify_decision_log_chain(self, now: datetime) -> None:
        del now
        async with self._session_factory() as session:
            use_case = VerifyDecisionLogChain(
                SqlDecisionLogRepository(session), ChainVerifier(), self._clock
            )
            report = await use_case.execute()
        log = logger.info if report.chain_ok else logger.error
        log(
            "maintenance_decision_log_chain_verified",
            chain_ok=report.chain_ok,
            verified_through_seq=report.verified_through_seq,
        )

    async def reconcile_platform_vs_crm(
        self, business_id: BusinessId, cycle_id: str, now: datetime
    ) -> None:
        del cycle_id
        cutoff = now - _RECONCILIATION_LAG
        async with self._session_factory() as session:
            use_case = ReconcilePlatformVsCrm(
                metrics=SqlMetricFactRepository(session),
                crm_conversions=SqlCrmConversionsAdapter(session),
                actionable_signals=SqlSignalReconciliationRepository(session),
                recorder=SqlSignalContradictionRecorderAdapter(session),
                clock=self._clock,
            )
            contradicted = await use_case.execute(business_id=business_id, cutoff=cutoff)
            await session.commit()
        logger.info(
            "maintenance_reconciled_platform_vs_crm",
            business_id=str(business_id),
            contradicted=len(contradicted),
        )
