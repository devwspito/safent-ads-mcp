"""Errores de aplicacion de `economics`: entidad no encontrada o
precondicion de puerto no cumplida (plan.md §8: 'ApplicationError')."""

from __future__ import annotations

from safent_ads.shared.errors import ApplicationError


class UnitEconomicsProfileNotFoundError(ApplicationError):
    """No hay `UnitEconomicsProfile` vigente para ese producto."""


class LagCurveNotFoundError(ApplicationError):
    """No hay curva de rezago materializada para `(product, platform)`."""


class PlatformDivergenceNotFoundError(ApplicationError):
    """No hay snapshot de divergencia para esa cuenta de plataforma."""


class OfferingPriceMissingError(ApplicationError):
    """`catalog` no tiene un `list_price` para este producto: ni siquiera
    un perfil `provisional` se puede construir sin precio (profitability-
    engine.md §1: `provisional_from_price_only` exige `list_price`)."""


class OfferingNotFoundError(ApplicationError):
    """No hay `offering` con ese id para ese `business_id` -- el borde HTTP
    lo mapea a 404 (nunca 403, misma politica IDOR que `require_business_
    access`: no revelar si el id existe en OTRO negocio)."""


class InvalidOfferingEconomicsError(ApplicationError):
    """`PUT /offerings/{id}/economics` con un valor fuera de rango
    (contracts/rest-api.md: '0 ≤ vat ≤ 100, money ≥ 0, refund 0–100').
    Revalidado aqui ademas de en el borde HTTP: el caso de uso no confia en
    que la unica llamante sea la ruta REST (defensa en profundidad)."""
