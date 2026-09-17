"""Advisory lock de Postgres: impide que dos procesos `ads-worker` corran
ciclos a la vez contra el mismo Postgres (threat-model.md C-15/C-17;
security review F2/F3, nit 4).

`compose.yaml` fija `ads-worker` a una sola replica -- sin lock entre
cuentas/ledger, dos workers claiming distintos intentos sobre la MISMA
entidad leen el mismo snapshot de `spend_ledger` en transacciones
separadas y ambos pasan el tope diario (C-15/C-17, "nit: no lock en el
ambito del guardarraíl"). Ese arreglo de fondo (lock por cuenta dentro de
`_pass_gates`) queda para mas adelante; este guard es la red de seguridad
mientras tanto -- si algo fuera de compose (escalado manual, un segundo
despliegue contra el mismo Postgres) incumple "un solo worker", el
recien llegado se niega a arrancar ciclos en vez de competir en silencio.

`pg_try_advisory_lock` es de SESION, no de transaccion: se libera solo en
cuanto la conexion que lo pidio se cierra (caida del proceso incluida),
nunca hace falta un `finally` a nivel de aplicacion para no dejarlo
huerfano."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from safent_ads.shared.errors import InfrastructureError

__all__ = ["SingleWorkerLock", "WorkerAlreadyRunningError"]

# Cualquier int64 fijo sirve de clave del ambito ("ads-worker corriendo
# ciclos"); arbitrario pero estable entre despliegues -- no se deriva de
# nada que pueda cambiar (version, host, PID), o un despliegue en curso
# invalidaria el lock del worker todavia en marcha.
_WORKER_CYCLES_LOCK_KEY = 875_190_442_101


class WorkerAlreadyRunningError(InfrastructureError):
    """Otro proceso `ads-worker` ya tiene el advisory lock de ciclos."""


class SingleWorkerLock:
    """Mantiene una conexion dedicada abierta mientras dure el proceso: es
    esa conexion, no una transaccion, la que posee el lock de sesion."""

    def __init__(self, engine: AsyncEngine, *, lock_key: int = _WORKER_CYCLES_LOCK_KEY) -> None:
        self._engine = engine
        self._lock_key = lock_key
        self._connection: AsyncConnection | None = None

    async def acquire(self) -> bool:
        """`True` si ESTA conexion se quedo con el lock. `pg_try_advisory_lock`
        nunca bloquea (a diferencia de `pg_advisory_lock`): si otra sesion
        ya lo tiene, devuelve `false` al instante -- lo que hace falta para
        que el worker decida "no arranco ciclos" en vez de colgarse."""
        connection = await self._engine.connect()
        result = await connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": self._lock_key}
        )
        acquired = bool(result.scalar_one())
        if not acquired:
            await connection.close()
            return False
        self._connection = connection
        return True

    async def release(self) -> None:
        if self._connection is None:
            return
        connection, self._connection = self._connection, None
        # `pg_advisory_unlock`, no solo `connection.close()`: el pool de
        # conexiones de SQLAlchemy no cierra la sesion real de Postgres al
        # devolver una conexion -- la deja viva para reutilizarla, asi que
        # el lock de sesion seguiria en pie hasta que el pool de verdad
        # desechase esa conexion (`engine.dispose()`, no garantizado a
        # tiempo). Liberar explicitamente es lo unico que funciona igual
        # con o sin pooling.
        await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": self._lock_key})
        await connection.close()
