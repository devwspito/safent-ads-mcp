"""Human editorial sign-off only; never creates platform execution authorizations."""

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.audit.application.record_decision import RecordDecision
from safent_ads.audit.domain.entry import ActorKind, DecisionKind, PendingDecision
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.shared.ids import BusinessId


class LaunchApprovalStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def status(self, business: str, slug: str, revision: str) -> dict[str, Any]:
        async with self.sessions() as session:
            row = (
                (
                    await session.execute(
                        text("""
                SELECT payload, occurred_at FROM decision_log
                WHERE business_id=:business AND event_type='launch_plan_reviewed'
                AND payload->>'slug'=:slug ORDER BY seq DESC LIMIT 1
            """),
                        {"business": UUID(business), "slug": slug},
                    )
                )
                .mappings()
                .first()
            )
        matched = bool(row and row["payload"].get("revision") == revision)
        return {
            "approved": matched,
            "approved_at": row["occurred_at"].isoformat() if matched and row else None,
        }

    async def approve(self, business: str, slug: str, revision: str, owner: UUID) -> dict[str, Any]:
        async with self.sessions() as session:
            await RecordDecision(SqlDecisionLogRepository(session)).execute(
                PendingDecision(
                    business_id=BusinessId.parse(business),
                    kind=DecisionKind.LAUNCH_PLAN_REVIEWED,
                    actor_kind=ActorKind.OWNER,
                    actor_id=str(owner),
                    payload={
                        "slug": slug,
                        "revision": revision,
                        "scope": "editorial-plan-only",
                        "authorizes_spend": False,
                    },
                )
            )
            await session.commit()
        return await self.status(business, slug, revision)
