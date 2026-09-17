"""`ConsumeFederatedTransaction` (002b threat-model.md C-75 pieza 2, T084
hardening): fase (a) del callback -- consumir la referencia de un solo uso
ANTES de tocar a Google.

Separada de `ResolveFederatedLogin` (`resolve_federated_login.py`) para que
la presentacion (`federated_router.py`) pueda cerrar esta sesion de BD nada
mas comprometer el consumo, hacer el canje contra Google (fase b) SIN
ninguna conexion de Postgres abierta, y abrir una sesion NUEVA para el
resto (fase c) -- antes, el `async with container.session_factory()` unico
envolvia las tres fases y una vuelta anonima retenia una conexion hasta los
10s del canje (NFR-103)."""

from __future__ import annotations

from safent_ads.iam.application.errors import FederatedTransactionInvalidError
from safent_ads.iam.application.ports import FederatedTransactionRepository
from safent_ads.iam.domain.federated_transaction import FederatedLoginTransaction, ReferenceHash
from safent_ads.shared.clock import Clock


class ConsumeFederatedTransaction:
    """Un solo `UPDATE ... RETURNING` de un solo uso (threat-model.md C-66):
    cero filas devueltas -> `FederatedTransactionInvalidError` SIN envolver
    -- no hay transaccion todavia de la que colgar un `txn_id`/`purpose`, y
    por eso esta es la UNICA excepcion del tramo federado que escapa tal
    cual (`federated_router.py::_unwrap`)."""

    def __init__(self, *, transactions: FederatedTransactionRepository, clock: Clock) -> None:
        self._transactions = transactions
        self._clock = clock

    async def execute(self, state: str) -> FederatedLoginTransaction:
        now = self._clock.now()
        transaction = await self._transactions.consume(state_hash=ReferenceHash.of(state), now=now)
        if transaction is None:
            raise FederatedTransactionInvalidError(
                "referencia federada desconocida, caducada o ya usada"
            )
        return transaction
