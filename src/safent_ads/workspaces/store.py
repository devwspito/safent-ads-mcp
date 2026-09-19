"""One authoritative store used by HTTP and MCP; no provider credentials or bypass."""

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.opportunities.domain.campaign_draft import DraftError, DraftFields
from safent_ads.opportunities.infrastructure.campaign_drafts_sql import CampaignDraftStore
from safent_ads.workspaces.adoption import ensure_workspace
from safent_ads.workspaces.contracts import CONTRACT_VERSION, WorkspaceBrief, campaign_step

LIST_LIMIT = 200


class WorkspaceStore:
    def __init__(self, drafts: CampaignDraftStore) -> None:
        self.drafts = drafts
        self.sessions = drafts._sessions

    async def _row(
        self, session: AsyncSession, business: str, identifier: str, *, lock: bool = False
    ) -> Mapping[Any, Any]:
        statement = "SELECT * FROM workspaces WHERE business_id=:b AND id=:id"
        if lock:
            statement += " FOR UPDATE"
        row = (
            (
                await session.execute(
                    text(statement),
                    {"b": UUID(business), "id": UUID(identifier)},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise DraftError("WORKSPACE_NOT_FOUND")
        return row

    @staticmethod
    def _view(row: Mapping[Any, Any]) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "business_id": str(row["business_id"]),
            "workspace_key": row["workspace_key"],
            "revision": row["revision"],
            "brief": WorkspaceBrief.model_validate(row["brief"]).model_dump(mode="json"),
            "updated_at": row["updated_at"].isoformat(),
            "contract_version": CONTRACT_VERSION,
        }

    async def _event(  # noqa: PLR0917 - scoped transaction and explicit audit identity
        self,
        session: AsyncSession,
        business: str,
        identifier: str,
        actor: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        await session.execute(
            text("""INSERT INTO workspace_events
            (business_id,workspace_id,actor,kind,payload)
            VALUES (:b,:id,:actor,:kind,CAST(:payload AS jsonb))"""),
            {
                "b": UUID(business),
                "id": UUID(identifier),
                "actor": actor,
                "kind": kind,
                "payload": json.dumps(payload),
            },
        )

    async def save(
        self,
        business: str,
        key: str,
        revision: int | None,
        changes: WorkspaceBrief,
        actor: str,
    ) -> dict[str, Any]:
        async with self.sessions.begin() as session:
            await self.drafts._business(session, business)
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": business + ":workspace:" + key},
            )
            row = (
                (
                    await session.execute(
                        text("""SELECT * FROM workspaces
                WHERE business_id=:b AND workspace_key=:key FOR UPDATE"""),
                        {"b": UUID(business), "key": key},
                    )
                )
                .mappings()
                .one_or_none()
            )
            previous = (
                WorkspaceBrief.model_validate(row["brief"]).model_dump(mode="json") if row else {}
            )
            brief = WorkspaceBrief.model_validate(
                {**previous, **changes.model_dump(mode="json", exclude_unset=True)}
            )
            if not brief.title:
                raise DraftError("WORKSPACE_TITLE_REQUIRED")
            payload = brief.model_dump(mode="json")
            if row is not None and payload == previous:
                return self._view(row)
            if row is not None and revision != row["revision"]:
                raise DraftError("WORKSPACE_CHANGED")
            if row is None and revision is not None:
                raise DraftError("WORKSPACE_NOT_FOUND")
            if row is None:
                identifier = await ensure_workspace(session, business, key, brief.title)
            else:
                identifier = str(row["id"])
            await session.execute(
                text("""UPDATE workspaces SET brief=CAST(:brief AS jsonb),
                revision=revision+:increment,updated_at=now() WHERE id=:id AND business_id=:b"""),
                {
                    "id": UUID(identifier),
                    "b": UUID(business),
                    "brief": json.dumps(payload),
                    "increment": int(row is not None),
                },
            )
            await self._event(
                session,
                business,
                identifier,
                actor,
                "brief_saved",
                {
                    "changes": changes.model_dump(mode="json", exclude_unset=True),
                    "approval_granted": False,
                },
            )
            return self._view(await self._row(session, business, identifier))

    async def list(self, business: str) -> dict[str, Any]:
        async with self.sessions() as session:
            await self.drafts._business(session, business)
            rows = (
                (
                    await session.execute(
                        text("""SELECT w.*,
                    (SELECT count(*) FROM campaign_drafts d
                     WHERE d.workspace_id=w.id AND d.business_id=w.business_id) AS campaign_count
                FROM workspaces w WHERE business_id=:b ORDER BY updated_at DESC,id LIMIT 201"""),
                        {"b": UUID(business)},
                    )
                )
                .mappings()
                .all()
            )
            return {
                "items": [
                    {**self._view(row), "campaign_count": row["campaign_count"]}
                    for row in rows[:200]
                ],
                "has_more": len(rows) > LIST_LIMIT,
                "contract_version": CONTRACT_VERSION,
            }

    async def get(self, business: str, identifier: str) -> dict[str, Any]:
        async with self.sessions() as session:
            view = self._view(await self._row(session, business, identifier))
            accounts = (
                (
                    await session.execute(
                        text("""SELECT account_ref,platform,
                external_account_id,currency,status FROM platform_accounts
                WHERE business_id=:b ORDER BY platform,external_account_id"""),
                        {"b": UUID(business)},
                    )
                )
                .mappings()
                .all()
            )
            rows = (
                (
                    await session.execute(
                        text("""SELECT * FROM campaign_drafts
                WHERE business_id=:b AND workspace_id=:id ORDER BY updated_at DESC LIMIT 201"""),
                        {"b": UUID(business), "id": UUID(identifier)},
                    )
                )
                .mappings()
                .all()
            )
            campaigns = []
            for row in rows[:200]:
                draft = self.drafts._view(row)
                proposal, execution = None, None
                if row["proposal_id"]:
                    p = (
                        (
                            await session.execute(
                                text("""SELECT id,state,diff_hash,expires_at
                        FROM proposals WHERE business_id=:b AND id=:id"""),
                                {"b": UUID(business), "id": row["proposal_id"]},
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if p:
                        proposal = {
                            "id": str(p["id"]),
                            "state": p["state"],
                            "diff_hash": p["diff_hash"],
                            "expires_at": p["expires_at"].isoformat(),
                        }
                    e = (
                        (
                            await session.execute(
                                text("""SELECT id,outcome,error_code,entity_ref,applied_value
                        FROM executions WHERE business_id=:b AND proposal_id=:id
                        ORDER BY created_at DESC LIMIT 1"""),
                                {"b": UUID(business), "id": row["proposal_id"]},
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if e:
                        execution = {
                            "id": str(e["id"]),
                            "outcome": e["outcome"],
                            "error_code": e["error_code"],
                            "entity_ref": e["entity_ref"],
                            "applied_value": e["applied_value"],
                        }
                campaigns.append(
                    {
                        "draft": draft,
                        "proposal": proposal,
                        "execution": execution,
                        "step": campaign_step(draft, proposal, execution),
                    }
                )
            events = (
                (
                    await session.execute(
                        text("""SELECT seq,actor,kind,payload,occurred_at
                FROM workspace_events WHERE business_id=:b AND workspace_id=:id
                ORDER BY seq DESC LIMIT 100"""),
                        {"b": UUID(business), "id": UUID(identifier)},
                    )
                )
                .mappings()
                .all()
            )
            jobs = (
                (
                    await session.execute(
                        text("""SELECT id,state,message,updated_at FROM runtime_jobs
                WHERE business_id=:b AND workspace_id=:id ORDER BY created_at DESC LIMIT 20"""),
                        {"b": UUID(business), "id": UUID(identifier)},
                    )
                )
                .mappings()
                .all()
            )
            return {
                **view,
                "accounts": [dict(account) for account in accounts],
                "campaigns": campaigns,
                "campaigns_has_more": len(rows) > LIST_LIMIT,
                "activity": [
                    {**dict(e), "occurred_at": e["occurred_at"].isoformat()} for e in events
                ],
                "runtime_jobs": [
                    {**dict(j), "id": str(j["id"]), "updated_at": j["updated_at"].isoformat()}
                    for j in jobs
                ],
                "capabilities": {
                    "shared_context": True,
                    "prepare_paused_proposal": True,
                    "automatic_activation": False,
                    "runtime_role": "draft_preparation",
                    "budget_is_enforced_cap": False,
                },
            }

    async def save_campaign(  # noqa: PLR0917 - same scoped optimistic command as draft store
        self,
        business: str,
        identifier: str,
        key: str,
        revision: int | None,
        changes: DraftFields,
        actor: str,
    ) -> dict[str, Any]:
        async with self.sessions.begin() as session:
            await self._row(session, business, identifier, lock=True)
            result = await self.drafts.save(
                business, key, revision, changes, transaction=session, workspace_id=identifier
            )
            await self._event(
                session,
                business,
                identifier,
                actor,
                "campaign_saved",
                {"draft_id": result["draft_id"], "revision": result["revision"]},
            )
        return result

    async def prepare(
        self,
        business: str,
        identifier: str,
        draft_id: str,
        revision: int,
        actor: str,
    ) -> dict[str, Any]:
        async with self.sessions.begin() as session:
            await self._row(session, business, identifier, lock=True)
            row = await self.drafts._row(session, business, draft_id, lock=True)
            if str(row["workspace_id"]) != identifier:
                raise DraftError("CAMPAIGN_DRAFT_NOT_FOUND")
            fields = DraftFields.model_validate(row["brief"])
            if fields.creation_plan and fields.creation_plan.get("status") != "PAUSED":
                raise DraftError("WORKSPACE_REQUIRES_PAUSED_PLAN")
            result = await self.drafts.promote(
                business,
                draft_id,
                revision,
                proposed_by=actor if actor.startswith("person:") else None,
                transaction=session,
            )
            if row["proposal_id"] is None:
                await self._event(
                    session,
                    business,
                    identifier,
                    actor,
                    "creation_proposed",
                    {
                        "draft_id": draft_id,
                        "proposal_id": result["proposal_id"],
                        "authorizes_spend": False,
                    },
                )
        return await self.get(business, identifier)
