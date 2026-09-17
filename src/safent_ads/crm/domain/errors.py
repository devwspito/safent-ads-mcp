"""Errores de dominio de `crm` (data-model.md §LeadAttribution: 'prohibido el
dato personal')."""

from __future__ import annotations

from safent_ads.shared.errors import DomainError


class BlankRawIdentifierError(DomainError):
    """El identificador crudo a hashear esta vacio."""


class BlankSaltError(DomainError):
    """La sal para hashear un identificador esta vacia."""


class BlankDigestError(DomainError):
    """El digest ya calculado rio arriba llega vacio."""


class InvalidIdentityDigestError(DomainError):
    """`identity_digest` no es un sha256 hexadecimal de 64 caracteres --
    ultima barrera de esquema antes de que un dato personal crudo entre en
    una fila que no debe tenerlo (contracts/crm-link.md §2)."""


class UnsupportedCurrencyError(DomainError):
    """Divisa fuera de ISO-4217 (3 letras mayusculas) o distinta de la del
    negocio -- adivinar la divisa esta prohibido (data-model.md §RevenueEvent)."""


class NonPositiveRevenueAmountError(DomainError):
    """`first_payment`/`recurring_payment` con importe <= 0: un cobro sin
    dinero no es un hecho economico representable."""


class RefundMustBeNegativeError(DomainError):
    """`refund` con importe >= 0 (contracts/crm-link.md: `400
    REFUND_MUST_BE_NEGATIVE`) -- una devolucion resta, nunca suma."""


class ChurnMustNotBePositiveError(DomainError):
    """`churn` con importe > 0: una baja no puede aportar contribucion
    positiva (data-model.md: 'refund y churn ⇒ amount <= 0')."""
