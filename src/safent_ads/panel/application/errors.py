"""Errores de aplicacion de `panel` (shared/errors.py: `ApplicationError`)."""

from __future__ import annotations

from safent_ads.shared.errors import ApplicationError


class PanelEntityNotFoundError(ApplicationError):
    """El identificador no existe. Mismo codigo/estado que "existe en otro
    negocio" (contracts/rest-api.md: "sesión sin acceso al negocio → 404,
    no 403, para no filtrar existencia")."""
