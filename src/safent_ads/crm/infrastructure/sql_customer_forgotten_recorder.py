"""`SqlCustomerForgottenRecorder` (spec 027 A-2): traduce a
`audit.application.record_decision.RecordDecision` -- el unico punto de
entrada al `decision_log` (mismo patron que `brand.infrastructure.
sql_brand_claims_decision_recorder.SqlBrandClaimsDecisionRecorder`).
`customer_hash` es el digest, nunca un identificador crudo -- el payload
pasa por `PendingDecision.__post_init__` (audit.domain.entry), que ya
rechaza claves como `email`/`phone`."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.audit.application.record_decision import RecordDecision
from safent_ads.audit.domain.entry import ActorKind, DecisionKind, PendingDecision
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.shared.ids import BusinessId

__all__ = ["SqlCustomerForgottenRecorder"]


class SqlCustomerForgottenRecorder:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self, *, business_id: BusinessId, customer_hash: str, rows_deleted: int
    ) -> None:
        async with self._session_factory() as session:
            recorder = RecordDecision(SqlDecisionLogRepository(session))
            await recorder.execute(
                PendingDecision(
                    business_id=business_id,
                    kind=DecisionKind.CUSTOMER_FORGOTTEN,
                    actor_kind=ActorKind.AGENT,
                    payload={"customer_hash": customer_hash, "rows_deleted": rows_deleted},
                )
            )
            await session.commit()
