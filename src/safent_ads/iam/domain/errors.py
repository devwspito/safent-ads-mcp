"""Errores de dominio de `iam` (data-model.md §Owner/Session,
threat-model.md C-25)."""

from __future__ import annotations

from safent_ads.shared.errors import DomainError


class TotpNotEnrolledError(DomainError):
    """El propietario aun no tiene un secreto TOTP confirmado: no puede
    iniciar sesion hasta completar el enrolamiento (fuera de banda)."""


class TotpAlreadyEnrolledError(DomainError):
    """Reenrolar sin revocar el secreto anterior no esta permitido."""


class SessionExpiredError(DomainError):
    """La sesion supero su ventana de inactividad o su tope absoluto."""


class SessionRevokedError(DomainError):
    """La sesion fue cerrada explicitamente (`Logout`)."""


class FederatedSessionWithoutIdentificationError(DomainError):
    """Sesion nacida de Google sin marca de identificacion federada (002b
    data-model.md §Session, CHECK `sessions_federated_origin_check`)."""


class UnknownFederatedIssuerError(DomainError):
    """Emisor federado fuera del vocabulario cerrado (hoy solo Google)."""


class InvalidFederatedSubjectError(DomainError):
    """El identificador estable del proveedor esta vacio o es demasiado largo."""


class FederatedIdentitySeenBeforeBindingError(DomainError):
    """`last_seen_at` anterior a `bound_at` (CHECK
    `owner_federated_identities_seen_check`)."""


class InvalidReferenceHashError(DomainError):
    """La cadena no es la huella sha256 hexadecimal de 64 caracteres de una
    referencia (`state`/`nonce`) del salto federado."""


class InvalidFederatedTransactionError(DomainError):
    """Transaccion federada que viola sus invariantes: caducidad no posterior
    a la creacion, o proposito y sesion que no se corresponden."""


class FederatedTransactionNotConsumableError(DomainError):
    """Consumir una transaccion federada ya consumida o caducada (FR-115: un
    solo uso, rechazo sin efectos)."""
