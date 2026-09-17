"""Adaptador real de `BrandClaimsDecisionRecorder` (`application/ports.py`):
traduce a `audit.application.record_decision.RecordDecision` -- el unico
punto de entrada que ese contexto expone (plan.md §4: `brand` puede
depender de `audit`, nunca al reves, mismo principio que documenta
`execution/infrastructure/decision_recorder.py`).

Sesion propia por llamada (mismo patron que
`brand.infrastructure.sql_brand_kit_repository.
RequestScopedBrandKitRepository`): `_build_brand_router` construye
`UpdateBrandClaims` una sola vez al arrancar la app, asi que esta
envoltura es la que hace que cada `record()` tenga su propia sesion y su
propio commit en vez de compartir uno entre peticiones."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.audit.application.record_decision import RecordDecision
from safent_ads.audit.domain.entry import ActorKind, DecisionKind, PendingDecision
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.shared.ids import BusinessId


class SqlBrandClaimsDecisionRecorder:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, *, business_id: BusinessId, actor_email: str) -> None:
        async with self._session_factory() as session:
            recorder = RecordDecision(SqlDecisionLogRepository(session))
            await recorder.execute(
                PendingDecision(
                    business_id=business_id,
                    kind=DecisionKind.BRAND_CLAIMS_UPDATED,
                    actor_kind=ActorKind.OWNER,
                    actor_id=actor_email,
                    payload={"event": "BrandClaimsUpdated"},
                )
            )
            await session.commit()
