"""`CreativeReadPort` real (mcp.application.ports) sobre `creative_briefs`/
`creative_jobs`/`creative_assets` (0021_creative_review). `status` de
`CreativeBriefSummary` no es una columna propia -- `creative_briefs` no
tiene estado (es un VO inmutable, plan.md §5): se deriva del `state` del
`CreativeJob` mas reciente de ese brief (`no_jobs` si todavia no se ha
encolado ninguno), mismo criterio que `SqlCatalogReadPort` deriva
`is_window_open` en vez de guardarlo."""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.mcp.application.dto import CreativeBriefSummary, CreativeJobStatus
from safent_ads.mcp.application.errors import EntityNotFoundError

_NO_JOBS_STATUS = "no_jobs"

_SELECT_BRIEFS = text("""
    SELECT b.id, b.source_signal_id, latest_job.state AS latest_job_state
      FROM creative_briefs b
      LEFT JOIN LATERAL (
          SELECT j.state
            FROM creative_jobs j
           WHERE j.brief_id = b.id
           ORDER BY j.created_at DESC
           LIMIT 1
      ) AS latest_job ON true
     WHERE b.business_id = :business_id
     ORDER BY b.created_at DESC
""")

_SELECT_JOB = text("""
    SELECT id, state, progress, asset_ids::text AS asset_ids_text
      FROM creative_jobs
     WHERE business_id = :business_id AND id = :job_id
""")

_SELECT_FIRST_ASSET_RENDERER = text("""
    SELECT renderer_used FROM creative_assets WHERE id = :asset_id
""")


class SqlCreativeReadPort:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_creative_briefs(self, business_id: str) -> list[CreativeBriefSummary]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(_SELECT_BRIEFS, {"business_id": business_id})
            ).mappings().all()
        return [_brief_summary(row) for row in rows]

    async def get_creative_job(self, business_id: str, job_id: str) -> CreativeJobStatus:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    _SELECT_JOB, {"business_id": business_id, "job_id": job_id}
                )
            ).mappings().one_or_none()
            if row is None:
                raise EntityNotFoundError(f"{job_id} aun no disponible")
            asset_ids = json.loads(row["asset_ids_text"])
            renderer_used = await self._first_renderer_used(session, asset_ids)
        return CreativeJobStatus(
            job_id=str(row["id"]),
            state=row["state"],
            progress=float(row["progress"]),
            renderer_used=renderer_used,
            asset_ids=[str(a) for a in asset_ids],
        )

    async def _first_renderer_used(
        self, session: AsyncSession, asset_ids: list[str]
    ) -> str | None:
        if not asset_ids:
            return None
        row = (
            await session.execute(_SELECT_FIRST_ASSET_RENDERER, {"asset_id": asset_ids[0]})
        ).mappings().one_or_none()
        return None if row is None else str(row["renderer_used"])


def _brief_summary(row: RowMapping) -> CreativeBriefSummary:
    return CreativeBriefSummary(
        brief_id=str(row["id"]),
        signal_id=str(row["source_signal_id"]) if row["source_signal_id"] is not None else None,
        status=row["latest_job_state"] or _NO_JOBS_STATUS,
    )
