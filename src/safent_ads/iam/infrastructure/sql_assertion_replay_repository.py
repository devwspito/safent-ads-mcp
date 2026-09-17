"""Adaptador SQL de `AssertionReplayGuard` sobre `sso_assertions_seen`
(0030_sso_assertions_seen, 026 sso.md §7 S-2). `claim()` purga las filas de
más de 24h antes de reclamar (misma llamada, sin cron nuevo) y confirma de
inmediato -- mismo motivo que `SqlLoginAttemptRepository.record()`: si el
canje falla despues por otro motivo (p. ej. `OWNER_BOUND_ELSEWHERE`) y la
sesion hace rollback al salir del `async with`, un `jti` ya visto NO puede
volver a quedar disponible para repetirse."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_PURGE_AFTER = timedelta(hours=24)

_PURGE_STALE_SQL = text("DELETE FROM sso_assertions_seen WHERE seen_at < :cutoff")
_CLAIM_JTI_SQL = text(
    """
    INSERT INTO sso_assertions_seen (jti, seen_at)
    VALUES (:jti, :seen_at)
    ON CONFLICT (jti) DO NOTHING
    RETURNING jti
    """
)


class SqlAssertionReplayRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(self, *, jti: str, seen_at: datetime) -> bool:
        await self._session.execute(_PURGE_STALE_SQL, {"cutoff": seen_at - _PURGE_AFTER})
        result = await self._session.execute(_CLAIM_JTI_SQL, {"jti": jti, "seen_at": seen_at})
        claimed = result.mappings().one_or_none() is not None
        await self._session.commit()
        return claimed
