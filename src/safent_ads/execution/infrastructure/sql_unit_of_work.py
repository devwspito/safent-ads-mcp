"""Adaptador SQL de `UnitOfWork`: la frontera transaccional de la ruta de
ejecucion (threat-model.md C-15, "guardarraíl evaluado DENTRO de la
transaccion de reclamo").

Se **une** a la transaccion que ya abrio el reclamo de la cola en vez de
abrir una nueva. `ExecutionChokepoint.run_once` llama a
`ExecutionQueuePort.claim_next()` antes de entrar en el bloque `async with
self._uow`, y esa primera sentencia ya arranco la transaccion del
`AsyncSession` (autobegin de SQLAlchemy). Si este adaptador abriese otra,
el `FOR UPDATE SKIP LOCKED` del reclamo viviria en una transaccion distinta
de la evaluacion del guardarraíl y volveria el TOCTOU que C-15 cierra: dos
escrituras concurrentes podrian cruzar el tope.

Al salir del bloque sin excepcion se confirma: reclamo, veredicto y reserva
durable quedan firmes antes de la llamada remota. Otro bloque confirma RUNNING
antes del dispatch; la reserva protege la cuenta sin mantener el lock de cuenta
durante la red. La liquidacion toma el mismo lock y confirma ledger, reserva y
desenlace juntos. El caller confirma cualquier salida pendiente restante.

Reentrante a proposito: `AuthorizeRuleAction` y el chokepoint pueden anidar
el mismo `UnitOfWork` sin que el bloque interior confirme por su cuenta.

`lock_account` (security review F2/F3, C-15/C-17 "no lock en el ambito del
guardarraíl"): el `SingleWorkerLock` de sesion (`orchestration/`) solo
impide que dos procesos `ads-worker` compitan entre si -- el chokepoint
tambien es alcanzable desde `ads-api` (`apply_defensive_action`), que no
tiene ese guard. `pg_advisory_xact_lock` cierra el hueco real: dos
transacciones-puerta concurrentes sobre la MISMA cuenta (`ads-worker` contra
`ads-api`, o dos peticiones de `ads-api`) se serializan, mientras que
cuentas distintas siguen en paralelo (la clave del lock es
`hashtext('ads-account:' || platform_account_id)`, no un lock global). De
transaccion, no de sesion: Postgres lo libera solo con el commit/rollback de
`_pass_gates`, nunca hace falta soltarlo a mano ni arriesga quedar huerfano
si el proceso muere.

ADS-01 cierra la antigua ventana entre ese commit y el ledger: las reservas
activas cuentan en los topes y no caducan por timeout, lease ni cambio de fecha.
La reconciliacion usa recibos, nunca repite una mutacion incierta."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.execution.infrastructure.errors import UnknownEntityRefError
from safent_ads.shared.ids import EntityRef

__all__ = ["SqlUnitOfWork"]

# hashtext() devuelve int4; pg_advisory_xact_lock acepta bigint y lo
# convierte implicitamente. El prefijo "ads-account:" separa este ambito de
# lock de cualquier otro (p. ej. `SingleWorkerLock`) que algun dia use la
# misma tecnica con una clave numerica fija.
_LOCK_ACCOUNT = """
    SELECT pg_advisory_xact_lock(hashtext('ads-account:' || a.business_id::text || ':' ||
                                        a.platform || ':' || a.external_account_id))
      FROM ads_execution_targets e JOIN platform_accounts a ON a.id = e.platform_account_id
     WHERE e.entity_ref = :entity_ref
"""


class SqlUnitOfWork:
    """Implementa `execution.application.ports.UnitOfWork` sobre un
    `AsyncSession`. No crea la sesion: la recibe, para que todos los
    repositorios de la misma ruta compartan transaccion."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._depth = 0

    async def __aenter__(self) -> SqlUnitOfWork:
        if not self._session.in_transaction():
            await self._session.begin()
        self._depth += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._depth -= 1
        if self._depth > 0:
            return
        if exc_type is None:
            await self._session.commit()
            return
        # Denegar por defecto: si algo fallo evaluando las puertas, tampoco
        # queda en pie el reclamo — la fila vuelve a la cola intacta.
        await self._session.rollback()

    async def lock_account(self, entity_ref: EntityRef) -> None:
        """Primera sentencia dentro del bloque, antes de leer guardarrailes
        ni verificar la autorizacion: resuelve la cuenta de `entity_ref` y
        toma su `pg_advisory_xact_lock` en la MISMA transaccion que ya
        abrio el reclamo de la cola (ver docstring del modulo). Bloquea de
        verdad -- no es un `try`: una segunda transaccion-puerta sobre la
        misma cuenta espera aqui hasta que la primera confirme o deshaga."""
        result = await self._session.execute(text(_LOCK_ACCOUNT), {"entity_ref": str(entity_ref)})
        if result.first() is None:
            raise UnknownEntityRefError(
                f"{entity_ref} no esta en ad_entities: no hay cuenta que bloquear"
            )
