"""`FederatedLoginTransaction` (002b data-model.md §FederatedLoginTransaction,
tabla `federated_login_transactions`): el salto de ida y vuelta al proveedor
de identidad, de UN SOLO USO y con caducidad corta (FR-115).

`state` y `nonce` viven aqui solo como huella sha256 -- el valor en claro
existe unicamente en la URL que ve el navegador y en memoria durante la
peticion, igual que `oauth_tokens.code_hash` en 002. Por eso la vuelta compara
`sha256(nonce del id_token)` contra `nonce_hash`, nunca dos valores en claro:
en el callback ya no queda ningun claro que comparar.

El proposito no lo elige el cliente: `LOGIN` no tiene sesion que atar y
`REIDENTIFY` la tiene siempre, invariante que la base de datos repite en
`federated_login_transactions_session_iff_reidentify_check`."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from safent_ads.iam.domain.errors import (
    FederatedTransactionNotConsumableError,
    InvalidFederatedTransactionError,
    InvalidReferenceHashError,
)

_HEX_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class ReferenceHash:
    """Huella sha256 hexadecimal de una referencia (`state` o `nonce`) del
    salto federado. Una copia de la base de datos no entrega referencias
    utilizables."""

    value: str

    def __post_init__(self) -> None:
        if not _HEX_SHA256_PATTERN.match(self.value):
            raise InvalidReferenceHashError(
                f"se esperaba sha256 hexadecimal de 64 caracteres: {self.value!r}"
            )

    @classmethod
    def of(cls, raw: str) -> ReferenceHash:
        return cls(hashlib.sha256(raw.encode("utf-8")).hexdigest())

    def __str__(self) -> str:
        return self.value


class TransactionPurpose(StrEnum):
    LOGIN = "login"
    REIDENTIFY = "reidentify"


class FederatedLoginTransaction:
    def __init__(
        self,
        *,
        state_hash: ReferenceHash,
        nonce_hash: ReferenceHash,
        purpose: TransactionPurpose,
        session_id: uuid.UUID | None,
        txn_id: uuid.UUID | None,
        created_at: datetime,
        expires_at: datetime,
        ip_address: str | None = None,
        consumed_at: datetime | None = None,
    ) -> None:
        self._require_positive_ttl(created_at, expires_at)
        self._require_session_iff_reidentify(purpose, session_id)
        self.state_hash = state_hash
        self.nonce_hash = nonce_hash
        self.purpose = purpose
        self.session_id = session_id
        self.txn_id = txn_id
        self.created_at = created_at
        self.expires_at = expires_at
        self.ip_address = ip_address
        self.consumed_at = consumed_at

    @classmethod
    def open_for_login(
        cls,
        *,
        state_hash: ReferenceHash,
        nonce_hash: ReferenceHash,
        txn_id: uuid.UUID | None,
        created_at: datetime,
        ttl: timedelta,
        ip_address: str | None = None,
    ) -> FederatedLoginTransaction:
        return cls(
            state_hash=state_hash,
            nonce_hash=nonce_hash,
            purpose=TransactionPurpose.LOGIN,
            session_id=None,
            txn_id=txn_id,
            created_at=created_at,
            expires_at=created_at + ttl,
            ip_address=ip_address,
        )

    @classmethod
    def open_for_reidentification(
        cls,
        *,
        state_hash: ReferenceHash,
        nonce_hash: ReferenceHash,
        session_id: uuid.UUID,
        txn_id: uuid.UUID | None,
        created_at: datetime,
        ttl: timedelta,
        ip_address: str | None = None,
    ) -> FederatedLoginTransaction:
        return cls(
            state_hash=state_hash,
            nonce_hash=nonce_hash,
            purpose=TransactionPurpose.REIDENTIFY,
            session_id=session_id,
            ip_address=ip_address,
            txn_id=txn_id,
            created_at=created_at,
            expires_at=created_at + ttl,
        )

    @property
    def is_consumed(self) -> bool:
        return self.consumed_at is not None

    def is_consumable(self, now: datetime) -> bool:
        return not self.is_consumed and now < self.expires_at

    def consume(self, now: datetime) -> None:
        """Transicion `pendiente -> consumida`. El adaptador SQL la hace de
        forma atomica en un solo `UPDATE ... RETURNING`; aqui vive la misma
        regla para los dobles en memoria y para el dominio."""
        if not self.is_consumable(now):
            raise FederatedTransactionNotConsumableError(
                "transaccion federada ya consumida o caducada"
            )
        self.consumed_at = now

    def matches_nonce(self, nonce_hash: ReferenceHash) -> bool:
        return self.nonce_hash == nonce_hash

    @staticmethod
    def _require_positive_ttl(created_at: datetime, expires_at: datetime) -> None:
        if expires_at <= created_at:
            raise InvalidFederatedTransactionError(
                "la caducidad de la transaccion federada no es posterior a su creacion"
            )

    @staticmethod
    def _require_session_iff_reidentify(
        purpose: TransactionPurpose, session_id: uuid.UUID | None
    ) -> None:
        if (purpose is TransactionPurpose.REIDENTIFY) != (session_id is not None):
            raise InvalidFederatedTransactionError(
                f"proposito {purpose.value} incompatible con la sesion asociada"
            )
