"""Leased durable jobs. Lost acknowledgements/retries never create duplicate drafts."""

import asyncio
import hashlib
import json
import secrets
from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.iam.presentation.errors import ApiError
from safent_ads.opportunities.infrastructure.campaign_drafts_sql import CampaignDraftStore
from safent_ads.runtime.contracts import RuntimeResult

MAX_ATTEMPTS = 3


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class RuntimeJobError(ApiError):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(status_code=status, code=code, message=code)
        self.code = code


def runtime_error(code: str, status: int = 409) -> RuntimeJobError:
    return RuntimeJobError(code, status)


def job_view(row: Mapping[Any, Any], *, context: bool = False) -> dict[str, Any]:
    names = ("id", "slug", "revision", "state", "attempts", "message", "result")
    result = {name: str(row[name]) if name == "id" else row[name] for name in names}
    result.update({name: row[name].isoformat() for name in ("created_at", "updated_at")})
    result["lease_until"] = row["lease_until"].isoformat() if row["lease_until"] else None
    result["authorizes_spend"] = False
    if context:
        result["context"] = row["context"]
    return result


async def enqueue_job(session: AsyncSession, business: str, plan: dict[str, Any]) -> dict[str, Any]:
    """Called in the SAME transaction as editorial approval."""
    params = {"business": UUID(business), "slug": plan["slug"], "revision": plan["revision"]}
    # A new approved revision supersedes unfinished work, fencing late reports.
    await session.execute(
        text("""UPDATE runtime_jobs SET state='cancelled',
        message='Sustituido por una nueva versión aprobada.', updated_at=now()
        WHERE business_id=:business AND slug=:slug AND revision<>:revision
          AND state IN ('queued','running','blocked','failed')"""),
        params,
    )
    row = (
        (
            await session.execute(
                text("""INSERT INTO runtime_jobs
        (id,business_id,slug,revision,context) VALUES
        (:id,:business,:slug,:revision,CAST(:context AS jsonb))
        ON CONFLICT(business_id,slug,revision) DO UPDATE SET slug=EXCLUDED.slug
        RETURNING *"""),
                {**params, "id": uuid4(), "context": json.dumps(plan)},
            )
        )
        .mappings()
        .one()
    )
    return job_view(row)


class RuntimeJobStore:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        drafts: CampaignDraftStore,
        current_revision: Callable[[str, str], str] | None = None,
    ) -> None:
        self.sessions, self.drafts = sessions, drafts
        self.current_revision = current_revision

    async def list(self, business: str, slug: str | None = None) -> dict[str, Any]:
        async with self.sessions() as session:
            rows = (
                (
                    await session.execute(
                        text("""SELECT * FROM runtime_jobs
                WHERE business_id=:business AND (CAST(:slug AS text) IS NULL OR slug=:slug)
                ORDER BY created_at DESC LIMIT 100"""),
                        {"business": UUID(business), "slug": slug},
                    )
                )
                .mappings()
                .all()
            )
        return {"items": [job_view(row) for row in rows]}

    async def get(self, business: str, job: str) -> dict[str, Any]:
        async with self.sessions() as session:
            return job_view(await self._row(session, business, job), context=True)

    async def claim(self, business: str, holder: str) -> dict[str, Any]:
        async with self.sessions.begin() as session:
            await session.execute(
                text("""UPDATE runtime_jobs SET state='failed',
                message='El runtime perdió la conexión tres veces. Reintento manual requerido.',
                updated_at=now() WHERE business_id=:business AND state='running'
                AND lease_until<now() AND attempts>=:maximum"""),
                {"business": UUID(business), "maximum": MAX_ATTEMPTS},
            )
            row = (
                (
                    await session.execute(
                        text("""SELECT * FROM runtime_jobs
                WHERE business_id=:business AND
                  (state='queued' OR (state='running' AND lease_until<now()))
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"""),
                        {"business": UUID(business)},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return {"job": None}
            if not await self._current(row):
                await session.execute(
                    text("""UPDATE runtime_jobs SET state='cancelled',
                    message='El plan ha cambiado; requiere una nueva aprobación.',updated_at=now()
                    WHERE id=:id"""),
                    {"id": row["id"]},
                )
                return {"job": None}
            lease = secrets.token_urlsafe(32)
            row = (
                (
                    await session.execute(
                        text("""UPDATE runtime_jobs SET state='running',
                holder=:holder,lease_hash=:lease,lease_until=now()+interval '120 seconds',
                attempts=attempts+1, message='Runtime conectado; preparando borrador.',
                updated_at=now() WHERE id=:id RETURNING *"""),
                        {"id": row["id"], "holder": holder, "lease": digest(lease)},
                    )
                )
                .mappings()
                .one()
            )
            result = job_view(row, context=True)
            result["lease_token"] = lease
        # Existing drafts are context, never commands or implicit authorization.
        result["existing_drafts"] = (await self.drafts.list(business))["items"]
        return {"job": result}

    async def heartbeat(
        self, business: str, job: str, holder: str, lease: str, message: str
    ) -> dict[str, Any]:
        async with self.sessions.begin() as session:
            await self._leased(session, business, job, holder, lease)
            row = (
                (
                    await session.execute(
                        text("""UPDATE runtime_jobs SET
                lease_until=now()+interval '120 seconds',message=:message,updated_at=now()
                WHERE id=:id RETURNING *"""),
                        {"id": UUID(job), "message": message[:2000]},
                    )
                )
                .mappings()
                .one()
            )
        return job_view(row)

    async def report(
        self, business: str, job: str, holder: str, lease: str, report: RuntimeResult
    ) -> dict[str, Any]:
        async with self.sessions.begin() as session:
            row = await self._row(session, business, job)
            self._holder(row, holder, lease)
            if row["state"] == "cancelled" or not await self._current(row):
                raise runtime_error("RUNTIME_PLAN_CHANGED")
            report_hash = digest(report.model_dump_json())
            if row["result"] and row["result"].get("report_hash") == report_hash:
                return job_view(row)  # A lost response can be acknowledged safely.
            await self._leased(session, business, job, holder, lease)
            draft = None
            if report.campaign is not None:
                # Stable job-owned key, same transaction, normal draft validation.
                draft = await self.drafts.save(
                    business,
                    "runtime-" + UUID(job).hex,
                    report.expected_draft_revision,
                    report.campaign,
                    transaction=session,
                )
            blockers = list(report.blockers)
            if draft:
                blockers.extend("Falta en el borrador: " + name for name in draft["missing_fields"])
            state = "blocked" if blockers and report.outcome == "prepared" else report.outcome
            result = {
                "summary": report.summary,
                "blockers": list(dict.fromkeys(blockers)),
                "draft_id": draft["draft_id"] if draft else None,
                "draft_revision": draft["revision"] if draft else None,
                "report_hash": report_hash,
                "published": False,
                "activation_blockers": row["context"].get("blockers", []),
            }
            row = (
                (
                    await session.execute(
                        text("""UPDATE runtime_jobs SET state=:state,
                message=:message,result=CAST(:result AS jsonb),lease_until=NULL,updated_at=now()
                WHERE id=:id RETURNING *"""),
                        {
                            "id": UUID(job),
                            "state": state,
                            "message": report.summary,
                            "result": json.dumps(result),
                        },
                    )
                )
                .mappings()
                .one()
            )
        return job_view(row)

    async def retry(self, business: str, job: str, message: str = "") -> dict[str, Any]:
        async with self.sessions.begin() as session:
            row = await self._row(session, business, job)
            if row["state"] not in {"blocked", "failed"}:
                raise runtime_error("RUNTIME_JOB_NOT_RETRYABLE")
            if not await self._current(row):
                raise runtime_error("RUNTIME_PLAN_CHANGED")
            # Keep the persisted draft as context; the next attempt must edit it
            # through a fresh revision, not silently replace it with a new draft.
            row = (
                (
                    await session.execute(
                        text("""UPDATE runtime_jobs SET state='queued',
                holder=NULL,lease_hash=NULL,lease_until=NULL,attempts=0,
                message='Reintento solicitado. Esperando runtime.',updated_at=now(),
                context=jsonb_set(context,'{operator_reply}',CAST(:reply AS jsonb))
                WHERE id=:id RETURNING *"""),
                        {"id": UUID(job), "reply": json.dumps(message)},
                    )
                )
                .mappings()
                .one()
            )
        return job_view(row)

    async def cancel(self, business: str, job: str) -> dict[str, Any]:
        async with self.sessions.begin() as session:
            row = await self._row(session, business, job)
            if row["state"] not in {"queued", "running", "blocked", "failed"}:
                raise runtime_error("RUNTIME_JOB_NOT_CANCELLABLE")
            row = (
                (
                    await session.execute(
                        text("""UPDATE runtime_jobs SET state='cancelled',
                lease_until=NULL,message='Cancelado por el propietario.',updated_at=now()
                WHERE id=:id RETURNING *"""),
                        {"id": UUID(job)},
                    )
                )
                .mappings()
                .one()
            )
        return job_view(row)

    async def _row(self, session: AsyncSession, business: str, job: str) -> Mapping[Any, Any]:
        row = (
            (
                await session.execute(
                    text("""SELECT *, lease_until>now() AS lease_valid
            FROM runtime_jobs WHERE business_id=:business AND id=:id FOR UPDATE"""),
                    {"business": UUID(business), "id": UUID(job)},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise runtime_error("RUNTIME_JOB_NOT_FOUND", 404)
        return row

    async def _current(self, row: Mapping[Any, Any]) -> bool:
        if self.current_revision is None:
            return True
        try:
            revision = await asyncio.to_thread(
                self.current_revision, row["slug"], str(row["business_id"])
            )
        except ApiError:
            return False
        return bool(revision == row["revision"])

    @staticmethod
    def _holder(row: Mapping[Any, Any], holder: str, lease: str) -> None:
        if row["holder"] != holder or not secrets.compare_digest(
            row["lease_hash"] or "", digest(lease)
        ):
            raise runtime_error("RUNTIME_LEASE_LOST")

    async def _leased(
        self, session: AsyncSession, business: str, job: str, holder: str, lease: str
    ) -> Mapping[Any, Any]:
        row = await self._row(session, business, job)
        self._holder(row, holder, lease)
        if row["state"] != "running" or not row["lease_valid"]:
            raise runtime_error("RUNTIME_LEASE_LOST")
        return row
