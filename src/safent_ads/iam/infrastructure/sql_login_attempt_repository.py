"""Adaptador SQL de `LoginAttemptRepository` sobre `login_attempts`
(0001_bootstrap.py, threat-model.md C-25: bloqueo 5/15 min por
propietario+ip).

`record()` confirma su propia escritura de inmediato (unidad de trabajo
propia, independiente de la sesion de la peticion): `Login`/`VerifyTotp`
llaman a `record()` y a continuacion lanzan (`InvalidCredentialsError`),
y el router (`iam/presentation/router.py`) traduce esa excepcion a
`ApiError` sin llegar nunca a su `await db_session.commit()` -- al salir
`async with container.session_factory() as db_session` por una excepcion,
`AsyncSession.close()` hace rollback de lo pendiente. Sin este commit
propio, el intento fallido nunca se persistia y el bloqueo 5/15 min
(threat-model.md C-25) no avanzaba nunca."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_RECORD_SQL = text(
    """
    INSERT INTO login_attempts (email, succeeded, ip_address)
    VALUES (:email, :succeeded, CAST(:ip_address AS INET))
    """
)
_COUNT_RECENT_FAILURES_SQL = text(
    """
    SELECT COUNT(*) AS failures
    FROM login_attempts
    WHERE email = :email
      AND ip_address = CAST(:ip_address AS INET)
      AND succeeded = false
      AND attempted_at >= :since
    """
)


class SqlLoginAttemptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, *, email: str, succeeded: bool, ip_address: str | None) -> None:
        await self._session.execute(
            _RECORD_SQL, {"email": email, "succeeded": succeeded, "ip_address": ip_address}
        )
        await self._session.commit()

    async def count_recent_failures(
        self, *, email: str, ip_address: str | None, since: datetime
    ) -> int:
        result = await self._session.execute(
            _COUNT_RECENT_FAILURES_SQL,
            {"email": email, "ip_address": ip_address, "since": since},
        )
        return int(result.scalar_one())
