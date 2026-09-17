"""Errores de dominio de `brand`."""

from __future__ import annotations

from safent_ads.shared.errors import DomainError


class BlankFieldError(DomainError):
    """Un campo obligatorio de texto llego vacio o solo con espacios."""


class InvalidHexColorError(DomainError):
    """Color fuera del formato `#RRGGBB`."""


class InvalidContrastRatioError(DomainError):
    """`contrast_ratio_on_white` fuera del rango valido `[1, 21]` (escala WCAG)."""


class MissingBaselineForbiddenClaimsError(DomainError):
    """`forbidden_claims` no incluye el suelo de seguridad
    (`DEFAULT_FORBIDDEN_CLAIMS`): ningun kit de marca puede permitir de
    nuevo un reclamo que el producto prohibe siempre."""


class AllowedClaimConflictsWithForbiddenError(DomainError):
    """`BrandKit.replace_claims`: un reclamo de `claims_allowlist`
    coincide con uno de `forbidden_claims` (incluido el suelo) -- el
    propietario no puede permitir a la vez lo que el kit prohibe."""


class InvalidConfidenceError(DomainError):
    """Confianza de un candidato de `BrandDiscoveryDraft` fuera de `[0, 1]`."""


class InvalidDiscoveryUrlError(DomainError):
    """URL de sitio web no apta para rastreo (threat-model.md C-11/C-12):
    esquema no http/https, sin host, literal IP, o con credenciales
    embebidas (`user:pass@host`, vector de confusion de host)."""


class PiiDetectedInCopySampleError(DomainError):
    """Una muestra de copy todavia contiene un email o telefono tras el
    saneado del extractor (defensa en profundidad: el saneado real ocurre
    en `infrastructure/website_brand_extractor.py`, pero el dominio nunca
    confia ciegamente en la capa de arriba)."""
