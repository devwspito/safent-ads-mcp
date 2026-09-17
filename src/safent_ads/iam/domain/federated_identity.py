"""`FederatedIdentity` (002b data-model.md §FederatedIdentity, tabla
`owner_federated_identities`): vinculo entre una cuenta del proveedor y el
dueno, atado SIEMPRE al identificador estable (`sub`), nunca al correo -- el
correo cambia, el `sub` no. `email_at_binding` es auditoria del alta, no la
clave de busqueda.

El vocabulario de emisores es cerrado (hoy solo Google) y lo canonicaliza
`FederatedIssuer.parse`: Google emite `iss` en dos formas equivalentes
(`https://accounts.google.com` y `accounts.google.com`) y las dos tienen que
acabar en la MISMA fila, o el mismo dueno acabaria con dos identidades."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from safent_ads.iam.domain.email import Email
from safent_ads.iam.domain.errors import (
    FederatedIdentitySeenBeforeBindingError,
    InvalidFederatedSubjectError,
    UnknownFederatedIssuerError,
)

_MAX_SUBJECT_LENGTH = 255


class FederatedIssuer(StrEnum):
    """Emisor admitido. El valor es la forma canonica que se persiste y que
    exige el CHECK `owner_federated_identities_issuer_check`."""

    GOOGLE = "https://accounts.google.com"

    @classmethod
    def parse(cls, raw: str) -> FederatedIssuer:
        issuer = _ISSUER_FORMS.get(raw.strip())
        if issuer is None:
            raise UnknownFederatedIssuerError(f"emisor federado no admitido: {raw!r}")
        return issuer


_ISSUER_FORMS: dict[str, FederatedIssuer] = {
    "https://accounts.google.com": FederatedIssuer.GOOGLE,
    "accounts.google.com": FederatedIssuer.GOOGLE,
}


@dataclass(frozen=True, slots=True)
class FederatedSubject:
    """Identificador estable de la cuenta en el proveedor (`sub`). Inmutable
    tras el alta."""

    value: str

    def __post_init__(self) -> None:
        normalized = self.value.strip()
        if not normalized or len(normalized) > _MAX_SUBJECT_LENGTH:
            raise InvalidFederatedSubjectError(
                f"identificador federado invalido de {len(normalized)} caracteres"
            )
        object.__setattr__(self, "value", normalized)

    def __str__(self) -> str:
        return self.value


class FederatedIdentity:
    def __init__(
        self,
        *,
        owner_id: uuid.UUID,
        issuer: FederatedIssuer,
        subject: FederatedSubject,
        email_at_binding: Email,
        bound_at: datetime,
        last_seen_at: datetime,
    ) -> None:
        self._require_seen_after_binding(bound_at, last_seen_at)
        self.owner_id = owner_id
        self.issuer = issuer
        self.subject = subject
        self.email_at_binding = email_at_binding
        self.bound_at = bound_at
        self.last_seen_at = last_seen_at

    def matches(self, *, issuer: FederatedIssuer, subject: FederatedSubject) -> bool:
        return self.issuer is issuer and self.subject == subject

    def record_seen(self, at: datetime) -> None:
        self._require_seen_after_binding(self.bound_at, at)
        self.last_seen_at = at

    @staticmethod
    def _require_seen_after_binding(bound_at: datetime, last_seen_at: datetime) -> None:
        if last_seen_at < bound_at:
            raise FederatedIdentitySeenBeforeBindingError(
                "last_seen_at anterior a bound_at en una identidad federada"
            )
