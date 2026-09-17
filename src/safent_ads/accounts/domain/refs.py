"""Identificadores de `accounts` que cruzan hacia `application`/`broker`:
`AccountRef` (cuenta de plataforma), `CredentialRefId` (alias opaco resuelto
solo por `ads-broker`, data-model.md `CredentialRef`) e `IdempotencyKey`
(contracts/platform-port.md)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from safent_ads.shared.errors import DomainError
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode


class InvalidIdempotencyKeyError(DomainError):
    """`IdempotencyKey` vacia."""


class AccountRefFormatError(DomainError):
    """La cadena no respeta el formato `<platform>:<external_account_id>`."""


@dataclass(frozen=True, slots=True)
class AccountRef:
    """Referencia canonica a una `PlatformAccount`: `(platform,
    external_account_id)`, unica global (data-model.md invariante). Al no
    tener la tabla `platform_accounts` un identificador propio expuesto
    por `AccountRepository`, esta cadena es el `platform_account_id` que
    usa la superficie REST de conexiones (`accounts/presentation`)."""

    platform: PlatformCode
    external_account_id: str
    business_id: uuid.UUID | None = None
    connection_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if (self.business_id is None) != (self.connection_id is None):
            raise AccountRefFormatError("business_id y connection_id deben viajar juntos")

    @classmethod
    def parse(cls, raw: str) -> AccountRef:
        if ":account:" in raw:
            try:
                entity = EntityRef.parse(raw)
            except ValueError as exc:
                raise AccountRefFormatError("referencia de cuenta inválida") from exc
            if entity.level != EntityLevel.ACCOUNT or entity.connection_id is None:
                raise AccountRefFormatError("referencia de cuenta sin conexión")
            return cls(
                entity.platform, entity.external_id, entity.business_id, entity.connection_id
            )
        platform_raw, separator, external_id = raw.partition(":")
        if not separator or not external_id:
            raise AccountRefFormatError(f"formato invalido: {raw!r}")
        try:
            platform = PlatformCode(platform_raw)
        except ValueError as exc:
            raise AccountRefFormatError(f"formato invalido: {raw!r}") from exc
        return cls(platform=platform, external_account_id=external_id)

    def __str__(self) -> str:
        if self.connection_id is not None:
            return str(
                EntityRef(
                    self.platform,
                    EntityLevel.ACCOUNT,
                    self.external_account_id,
                    self.business_id,
                    self.connection_id,
                )
            )
        return f"{self.platform.value}:{self.external_account_id}"


@dataclass(frozen=True, slots=True)
class CredentialRefId:
    """Identidad opaca de un `CredentialRef`. Nunca contiene el secreto
    (data-model.md invariante)."""

    value: uuid.UUID

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class IdempotencyKey:
    """Clave de idempotencia de un intento de escritura
    (contracts/platform-port.md `execute_write`)."""

    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise InvalidIdempotencyKeyError("idempotency_key vacia")

    def __str__(self) -> str:
        return self.value
