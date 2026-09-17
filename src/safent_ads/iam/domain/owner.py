"""`Owner` aggregate (data-model.md §Owner/Session, tabla `owners`):
"Argon2id; TOTP obligatorio, cifrado en reposo". El dominio nunca calcula
hashes ni cifra nada -- `password_hash`/`totp_secret_encrypted` son blobs
opacos que produce la infraestructura (`iam/infrastructure/`); el dominio
solo conoce las reglas de cuando un login puede avanzar."""

from __future__ import annotations

import uuid
from datetime import datetime

from safent_ads.iam.domain.email import Email
from safent_ads.iam.domain.errors import TotpAlreadyEnrolledError, TotpNotEnrolledError


class Owner:
    def __init__(
        self,
        *,
        owner_id: uuid.UUID,
        email: Email,
        password_hash: str,
        totp_secret_encrypted: bytes | None,
        totp_confirmed_at: datetime | None,
        created_at: datetime,
    ) -> None:
        self.id = owner_id
        self.email = email
        self.password_hash = password_hash
        self.created_at = created_at
        self._totp_secret_encrypted = totp_secret_encrypted
        self._totp_confirmed_at = totp_confirmed_at

    @property
    def totp_secret_encrypted(self) -> bytes | None:
        return self._totp_secret_encrypted

    @property
    def totp_confirmed_at(self) -> datetime | None:
        return self._totp_confirmed_at

    @property
    def is_totp_enrolled(self) -> bool:
        return self._totp_confirmed_at is not None

    def require_totp_enrolled(self) -> None:
        if not self.is_totp_enrolled:
            raise TotpNotEnrolledError(f"owner {self.id} no tiene TOTP confirmado")

    def begin_totp_enrollment(self, encrypted_secret: bytes) -> None:
        if self.is_totp_enrolled:
            raise TotpAlreadyEnrolledError(f"owner {self.id} ya tiene TOTP confirmado")
        self._totp_secret_encrypted = encrypted_secret

    def confirm_totp_enrollment(self, confirmed_at: datetime) -> None:
        if self._totp_secret_encrypted is None:
            raise TotpNotEnrolledError("no hay secreto TOTP pendiente de confirmar")
        self._totp_confirmed_at = confirmed_at
