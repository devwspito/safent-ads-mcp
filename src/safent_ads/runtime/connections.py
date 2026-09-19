"""Narrow bridge credentials: only claim/report jobs, never ads/provider access."""

import secrets
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.runtime.store import digest, runtime_error

MIN_TOKEN_LENGTH = 32
MAX_TOKEN_LENGTH = 200


class RuntimeConnections:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self.sessions = sessions

    async def create(
        self, business: str, label: str, runtime: Literal["codex", "claude"]
    ) -> dict[str, Any]:
        async with self.sessions.begin() as session:
            return await self.create_in_session(session, business, label, runtime)

    async def create_in_session(
        self, session: AsyncSession, business: str, label: str, runtime: Literal["codex", "claude"]
    ) -> dict[str, Any]:
        token, identifier = secrets.token_urlsafe(48), uuid4()
        await session.execute(
            text("""INSERT INTO runtime_connections
                (id,business_id,label,runtime,token_hash) VALUES
                (:id,:business,:label,:runtime,:hash)"""),
            {
                "id": identifier,
                "business": UUID(business),
                "label": label,
                "runtime": runtime,
                "hash": digest(token),
            },
        )
        return {"id": str(identifier), "token": token, "expires_in_days": 30}

    async def list(self, business: str) -> dict[str, Any]:
        async with self.sessions() as session:
            rows = (
                (
                    await session.execute(
                        text("""SELECT id,label,runtime,expires_at,
                revoked_at,last_seen_at,
                (last_seen_at > now()-interval '90 seconds'
                 AND expires_at>now() AND revoked_at IS NULL) AS connected
                FROM runtime_connections WHERE business_id=:business
                ORDER BY created_at DESC LIMIT 100"""),
                        {"business": UUID(business)},
                    )
                )
                .mappings()
                .all()
            )
        return {
            "items": [
                {
                    "id": str(row["id"]),
                    "label": row["label"],
                    "runtime": row["runtime"],
                    "connected": bool(row["connected"]),
                    **{
                        name: row[name].isoformat() if row[name] else None
                        for name in ("expires_at", "revoked_at", "last_seen_at")
                    },
                }
                for row in rows
            ]
        }

    async def revoke(self, business: str, identifier: str) -> None:
        async with self.sessions.begin() as session:
            row = (
                await session.execute(
                    text("""UPDATE runtime_connections
                SET revoked_at=COALESCE(revoked_at,now())
                WHERE business_id=:business AND id=:id RETURNING id"""),
                    {"business": UUID(business), "id": UUID(identifier)},
                )
            ).first()
            if row is None:
                raise runtime_error("RUNTIME_CONNECTION_NOT_FOUND", 404)
            # Fence in-flight reports as soon as the credential is revoked.
            await session.execute(
                text("""UPDATE runtime_jobs SET state='failed',
                lease_hash=NULL,lease_until=NULL,message='Conector revocado.',updated_at=now()
                WHERE business_id=:business AND holder=:holder AND state='running'"""),
                {"business": UUID(business), "holder": "bridge:" + identifier},
            )

    async def authenticate(self, token: str, *, touch: bool = True) -> tuple[str, str]:
        if not MIN_TOKEN_LENGTH <= len(token) <= MAX_TOKEN_LENGTH:
            raise runtime_error("RUNTIME_CREDENTIAL_INVALID", 401)
        async with self.sessions.begin() as session:
            row = (
                await session.execute(
                    text("""UPDATE runtime_connections
                SET last_seen_at=CASE WHEN :touch THEN now() ELSE last_seen_at END
                WHERE token_hash=:hash
                  AND revoked_at IS NULL AND expires_at>now()
                RETURNING business_id,id"""),
                    {"hash": digest(token), "touch": touch},
                )
            ).first()
        if row is None:
            raise runtime_error("RUNTIME_CREDENTIAL_INVALID", 401)
        return str(row.business_id), "bridge:" + str(row.id)
