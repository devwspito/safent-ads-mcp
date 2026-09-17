"""Adaptador SQL de `FederatedTransactionRepository` sobre
`federated_login_transactions` (0052, spec 002b research.md Decision C).

El consumo es UNA sentencia: `UPDATE ... WHERE state_hash = :h AND
consumed_at IS NULL AND expires_at > :now RETURNING ...`. No hay `SELECT`
previo que comprobar y luego un `UPDATE` que aplicar -- ese patron tiene
una ventana entre los dos en la que dos vueltas simultaneas con el mismo
`state` pasarian las dos. Aqui el filtro y la escritura son la misma
operacion atomica: la segunda no encuentra fila (FR-115).

`state` y `nonce` solo entran hasheados; esta clase no sabe hashear ni
generar nada -- recibe huellas ya calculadas, como `SqlSessionRepository`
recibe `token_hash`."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.iam.domain.federated_transaction import (
    FederatedLoginTransaction,
    ReferenceHash,
    TransactionPurpose,
)

_INSERT_SQL = text(
    """
    INSERT INTO federated_login_transactions (state_hash, nonce_hash, purpose, session_id,
                                              txn_id, created_at, expires_at, ip_address)
    VALUES (:state_hash, :nonce_hash, :purpose, :session_id, :txn_id, :created_at, :expires_at,
            CAST(:ip_address AS INET))
    """
)

_CONSUME_SQL = text(
    """
    UPDATE federated_login_transactions
       SET consumed_at = :now
     WHERE state_hash = :state_hash
       AND consumed_at IS NULL
       AND expires_at > :now
    RETURNING state_hash, nonce_hash, purpose, session_id, txn_id,
              created_at, expires_at, consumed_at, host(ip_address) AS ip_address
    """
)

# `RETURNING` + contar filas devueltas en vez de `.rowcount`, criterio ya
# usado en `prune_stale_clients.py`: `CursorResult` generico no lo tipa.
_PURGE_SQL = text(
    "DELETE FROM federated_login_transactions WHERE expires_at < :now RETURNING state_hash"
)

# threat-model.md C-79: pendiente = ni consumida ni caducada. `ip_address =
# :ip_address` nunca casa con `NULL` (filas de antes de esta columna), asi
# que esas no cuentan contra ningun tope -- exactamente el comportamiento
# que se busca.
_COUNT_PENDING_FOR_IP_SQL = text(
    """
    SELECT count(*) AS pending
      FROM federated_login_transactions
     WHERE ip_address = CAST(:ip_address AS INET)
       AND consumed_at IS NULL
       AND expires_at > :now
    """
)


def _row_to_transaction(row: Any) -> FederatedLoginTransaction:  # noqa: ANN401 - fila de SQLAlchemy
    return FederatedLoginTransaction(
        state_hash=ReferenceHash(row.state_hash),
        nonce_hash=ReferenceHash(row.nonce_hash),
        purpose=TransactionPurpose(row.purpose),
        session_id=row.session_id,
        txn_id=row.txn_id,
        created_at=row.created_at,
        expires_at=row.expires_at,
        ip_address=row.ip_address,
        consumed_at=row.consumed_at,
    )


class SqlFederatedTransactionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, transaction: FederatedLoginTransaction) -> None:
        await self._session.execute(
            _INSERT_SQL,
            {
                "state_hash": str(transaction.state_hash),
                "nonce_hash": str(transaction.nonce_hash),
                "purpose": transaction.purpose.value,
                "session_id": _as_text(transaction.session_id),
                "txn_id": _as_text(transaction.txn_id),
                "created_at": transaction.created_at,
                "expires_at": transaction.expires_at,
                "ip_address": transaction.ip_address,
            },
        )
        await self._session.commit()

    async def count_pending_for_ip(self, *, ip_address: str, now: datetime) -> int:
        result = await self._session.execute(
            _COUNT_PENDING_FOR_IP_SQL, {"ip_address": ip_address, "now": now}
        )
        return int(result.scalar_one())

    async def consume(
        self, *, state_hash: ReferenceHash, now: datetime
    ) -> FederatedLoginTransaction | None:
        result = await self._session.execute(
            _CONSUME_SQL, {"state_hash": str(state_hash), "now": now}
        )
        row = result.one_or_none()
        # El commit va tambien cuando no hubo fila: cierra la transaccion de
        # base de datos en vez de dejarla abierta hasta el final de la
        # peticion (las transacciones largas bloquean el vacuum).
        await self._session.commit()
        return None if row is None else _row_to_transaction(row)

    async def purge_expired(self, now: datetime) -> int:
        purged = len((await self._session.execute(_PURGE_SQL, {"now": now})).all())
        await self._session.commit()
        return purged

    async def get(self, state_hash: ReferenceHash) -> FederatedLoginTransaction | None:
        """Solo para pruebas y diagnostico: leer una transaccion NUNCA
        autoriza nada -- quien decide es `consume`."""
        result = await self._session.execute(
            text(
                "SELECT state_hash, nonce_hash, purpose, session_id, txn_id, "
                "created_at, expires_at, consumed_at, host(ip_address) AS ip_address "
                "FROM federated_login_transactions WHERE state_hash = :state_hash"
            ),
            {"state_hash": str(state_hash)},
        )
        row = result.one_or_none()
        return None if row is None else _row_to_transaction(row)


def _as_text(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)
