"""Adopt legacy and new drafts into a workspace in their existing transaction."""

import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.workspaces.contracts import WorkspaceBrief


async def ensure_workspace(
    session: AsyncSession, business: str, key: str, title: str, source_slug: str | None = None
) -> str:
    brief = WorkspaceBrief(title=title[:160], source_slug=source_slug).model_dump(mode="json")
    return str(
        (
            await session.execute(
                text("""INSERT INTO workspaces
        (business_id,workspace_key,brief) VALUES (:b,:key,CAST(:brief AS jsonb))
        ON CONFLICT(business_id,workspace_key) DO UPDATE SET workspace_key=EXCLUDED.workspace_key
        RETURNING id"""),
                {"b": UUID(business), "key": key, "brief": json.dumps(brief)},
            )
        ).scalar_one()
    )
