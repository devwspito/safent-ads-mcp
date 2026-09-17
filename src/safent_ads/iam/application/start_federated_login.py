"""`StartFederatedLogin` (002b contracts/federated-login.md §1): abre el salto
al proveedor de identidad y devuelve la URL a la que navega el navegador.

El PROPOSITO lo decide el servidor, no el cliente: con sesion del panel viva
el salto re-identifica; sin ella, entra. La regla vive aqui -- y no en el
router -- para que ninguna ruta pueda saltarsela por accidente.

`state` y `nonce` se generan aqui, se persisten SOLO como huella y viajan en
claro unicamente dentro de `authorization_url`. La respuesta nunca los expone
como campos sueltos.

Validar el `txn_id` (que exista, este `PENDING` y sin caducar) es cosa de la
presentacion: esa transaccion pertenece a `mcp_oauth`, y las dependencias van
`mcp_oauth -> iam`, nunca al reves.

threat-model.md C-79: antes de abrir una transaccion nueva se cuentan las
que esa IP ya tiene pendientes (ni consumidas ni caducadas); al llegar al
tope (`MAX_PENDING_FEDERATED_TRANSACTIONS_PER_IP`) se rechaza SIN crear una
sexta -- mismo criterio que `mcp_oauth.application.policy.
MAX_PENDING_PER_CLIENT`."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime

from safent_ads.iam.application.errors import TooManyPendingFederatedTransactionsError
from safent_ads.iam.application.ports import (
    FederatedIdentityProvider,
    FederatedTransactionRepository,
)
from safent_ads.iam.application.session_policy import (
    FEDERATED_TRANSACTION_TTL,
    MAX_PENDING_FEDERATED_TRANSACTIONS_PER_IP,
)
from safent_ads.iam.domain.federated_transaction import (
    FederatedLoginTransaction,
    ReferenceHash,
)
from safent_ads.shared.clock import Clock

_REFERENCE_BYTES = 32  # 256 bits por referencia


@dataclass(frozen=True, slots=True, kw_only=True)
class StartedFederatedLogin:
    authorization_url: str
    expires_at: datetime


class StartFederatedLogin:
    def __init__(
        self,
        *,
        provider: FederatedIdentityProvider,
        transactions: FederatedTransactionRepository,
        clock: Clock,
        redirect_uri: str,
    ) -> None:
        self._provider = provider
        self._transactions = transactions
        self._clock = clock
        self._redirect_uri = redirect_uri

    async def execute(
        self, *, txn_id: uuid.UUID | None, session_id: uuid.UUID | None, ip_address: str | None
    ) -> StartedFederatedLogin:
        # Un solo `now` para toda la ejecucion (nit, code review 17-sep):
        # el tope por IP y la apertura de la transaccion deben ver el
        # MISMO instante, no dos lecturas del reloj a milisegundos de
        # distancia.
        now = self._clock.now()
        # `None` cuando `shared.net.client_ip.resolve_client_ip` no pudo
        # resolver ninguna IP valida (`request.client` ausente -- nunca
        # ocurre con un servidor ASGI real, code review 17-sep): el tope
        # por IP no tiene nada que contar y se salta, en vez de agrupar
        # todo lo irresoluble bajo una clave compartida.
        if ip_address is not None:
            await self._reject_if_ip_at_pending_capacity(ip_address, now=now)
        state = secrets.token_urlsafe(_REFERENCE_BYTES)
        nonce = secrets.token_urlsafe(_REFERENCE_BYTES)
        transaction = self._open(
            state=state,
            nonce=nonce,
            txn_id=txn_id,
            session_id=session_id,
            ip_address=ip_address,
            now=now,
        )
        await self._transactions.create(transaction)
        return StartedFederatedLogin(
            authorization_url=self._provider.authorization_url(
                state=state, nonce=nonce, redirect_uri=self._redirect_uri
            ),
            expires_at=transaction.expires_at,
        )

    async def _reject_if_ip_at_pending_capacity(self, ip_address: str, *, now: datetime) -> None:
        pending = await self._transactions.count_pending_for_ip(ip_address=ip_address, now=now)
        if pending >= MAX_PENDING_FEDERATED_TRANSACTIONS_PER_IP:
            raise TooManyPendingFederatedTransactionsError(
                f"{ip_address} supera {MAX_PENDING_FEDERATED_TRANSACTIONS_PER_IP} "
                "transacciones federadas pendientes"
            )

    def _open(
        self,
        *,
        state: str,
        nonce: str,
        txn_id: uuid.UUID | None,
        session_id: uuid.UUID | None,
        ip_address: str | None,
        now: datetime,
    ) -> FederatedLoginTransaction:
        state_hash, nonce_hash = ReferenceHash.of(state), ReferenceHash.of(nonce)
        if session_id is None:
            return FederatedLoginTransaction.open_for_login(
                state_hash=state_hash,
                nonce_hash=nonce_hash,
                txn_id=txn_id,
                created_at=now,
                ttl=FEDERATED_TRANSACTION_TTL,
                ip_address=ip_address,
            )
        return FederatedLoginTransaction.open_for_reidentification(
            state_hash=state_hash,
            nonce_hash=nonce_hash,
            session_id=session_id,
            txn_id=txn_id,
            created_at=now,
            ttl=FEDERATED_TRANSACTION_TTL,
            ip_address=ip_address,
        )
