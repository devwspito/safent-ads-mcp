"""Errores de aplicacion de `iam`. La presentacion los mapea por tipo a
codigos HTTP, nunca por mensaje (plan.md §8)."""

from __future__ import annotations

from enum import StrEnum

from safent_ads.iam.domain.email import Email
from safent_ads.shared.errors import ApplicationError


class InvalidCredentialsError(ApplicationError):
    """Email/password o codigo TOTP incorrectos. Mismo mensaje para ambos
    casos: no distinguir evita filtrar que parte fallo."""


class AccountLockedError(ApplicationError):
    """5 intentos fallidos en 15 min (threat-model.md C-25)."""


class InvalidChallengeError(ApplicationError):
    """`challenge_id` de `/auth/totp` ilegible, con firma invalida o
    caducado."""


class AssertionMalformedError(ApplicationError):
    """`POST /auth/exchange` (026, contracts/sso.md §4): formato/base64/JSON
    de la aserción invalidos -- ni siquiera se llega a verificar la firma."""


class AssertionInvalidError(ApplicationError):
    """Firma Ed25519 invalida o campos literales (`v`/`iss`/`aud`/`slug`/
    `purpose`) que no coinciden con lo esperado (sso.md §4)."""


class AssertionExpiredError(ApplicationError):
    """`iat`/`exp` fuera de la ventana de 60s de la aserción (sso.md §3)."""


class AssertionReplayedError(ApplicationError):
    """El `jti` de la aserción ya fue canjeado antes (sso.md §7 S-2)."""


class OwnerBoundElsewhereError(ApplicationError):
    """El propietario único ya está atado a un `sub` distinto del que trae
    la aserción (sso.md §4: TOFU, atado a otro -> 403)."""


class FederatedDenialReason(StrEnum):
    """Motivo REAL de una denegacion federada. Vive en el registro interno y
    en la auditoria; hacia el navegador los dos colapsan en el mismo codigo
    `denied` (002b SC-103: el mensaje no delata si el correo estaba en la
    lista)."""

    EMAIL_NOT_VERIFIED = "email_not_verified"
    EMAIL_NOT_ALLOWED = "email_not_allowed"


class FederatedIdentityNotAuthorizedError(ApplicationError):
    """Correo sin verificar o fuera de la lista de autorizados (002b FR-102):
    ni sesion, ni alta, ni marca de frescura.

    Lleva el correo del `id_token` YA validado (threat-model.md C-77): un
    fallo federado solo puede alimentar el bloqueo 5/15 min contra ESE
    correo, nunca contra el del dueno de la instalacion -- de lo contrario
    un anonimo probando cuentas de Google ajenas podria dejar al dueno
    fuera con solo cinco peticiones."""

    def __init__(self, reason: FederatedDenialReason, email: Email) -> None:
        super().__init__(f"identidad federada no autorizada: {reason.value}")
        self.reason = reason
        self.email = email


class FederatedTransactionInvalidError(ApplicationError):
    """Referencia del salto federado desconocida, caducada o ya consumida, o
    la sesion que la abrio ya no esta activa (002b FR-115): rechazo SIN
    efectos."""


class FederatedIdentityMismatchError(ApplicationError):
    """Al re-identificar, la identidad federada resuelta apunta a un dueno
    distinto del de la sesion que abrio el salto (002b research.md Decision F:
    una vuelta nunca marca frescura en otra sesion ni en otro dueno)."""


class TooManyPendingFederatedTransactionsError(ApplicationError):
    """Threat-model.md C-79: ya hay 5 transacciones federadas pendientes
    (ni consumidas ni caducadas) abiertas desde esta IP -- mismo criterio
    que `mcp_oauth.application.policy.MAX_PENDING_PER_CLIENT`. `/start`
    rechaza SIN abrir una sexta, antes de tocar al proveedor."""


class ConsentTransactionNotFoundError(ApplicationError):
    """T085: el `txn_id` de consentimiento (una `AuthorizationRequest` de
    `mcp_oauth`) no existe. Vive aqui, generica y sin conocer `mcp_oauth`,
    para que `iam.application.ports.OpenConsentTransactions` pueda declarar
    su contrato sin importar el modulo que de verdad resuelve la pregunta
    (plan.md "mcp_oauth -> iam, nunca al reves")."""


class ConsentTransactionNotOpenError(ApplicationError):
    """T085: el `txn_id` existe pero ya no esta `PENDING` o ya caduco --
    mismo desenlace, `410 TXN_EXPIRED`, para las dos causas (opacidad ante
    cual de las dos fue)."""
