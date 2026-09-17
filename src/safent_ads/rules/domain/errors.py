"""Errores de dominio de `rules`."""

from __future__ import annotations

from safent_ads.shared.errors import DomainError


class SpendIncreasingAutoRuleError(DomainError):
    """Una regla `AUTO` cuya accion sube gasto (spec.md FR-11/FR-12; out of
    scope: 'autonomia sobre acciones que aumenten gasto prohibida en v1').
    El dominio la rechaza en construccion, no en un test aparte."""


class BlankRuleCodeError(DomainError):
    """El `code` de una regla esta vacio."""


class EmptyConditionError(DomainError):
    """Una `Condition` sin ninguna clausula no puede evaluarse."""


class InvalidCooldownError(DomainError):
    """`cooldown` negativo."""


class InvalidGuardrailPolicyError(DomainError):
    """`GuardrailPolicy` con floor > ceiling, `max_step_pct` fuera de rango,
    o un tope negativo."""


class InvalidBrakeScopeError(DomainError):
    """`BrakeScope` incoherente: el ambito global no nombra negocio ni
    cuenta, y los otros dos nombran exactamente el suyo."""
