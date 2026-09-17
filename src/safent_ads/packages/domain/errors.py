"""Excepciones propias de `packages.domain` (data-model.md). Cada mensaje
es un codigo estable -- nunca texto libre del proveedor ni una traza --
igual que `proposals.domain.campaign_creation.CampaignCreationError`."""

from __future__ import annotations

from safent_ads.shared.errors import DomainError


class PackageDomainError(DomainError):
    """Raiz de las excepciones de `packages.domain`."""


class PlannedTreeError(PackageDomainError):
    """Un value object o entidad del arbol declarado no respeta su forma
    (p. ej. `local_ref` mal formado, texto vacio, cotas de longitud)."""


class PackageStructureError(PackageDomainError):
    """El arbol completo del paquete viola una cota estructural: numero de
    conjuntos/anuncios, `local_ref` duplicado o fuera de posicion, alcance
    de cuenta/plataforma (data-model.md invariantes 1 y 2)."""


class PlatformCompletenessError(PackageDomainError):
    """Delegado desde `creation_budget`/`validate_child_payload`
    (invariante 6): falta una eleccion nativa obligatoria o el plan no
    respeta el formato exigido por la plataforma."""


class PackageBudgetError(PackageDomainError):
    """El dinero declarado no respeta el tope diario de cuenta o el sobre
    disponible (invariante 5)."""


class CampaignPackageInvariantError(PackageDomainError):
    """Transicion de estado invalida o precondicion de agregado incumplida."""


class PackageHashMismatchError(PackageDomainError):
    """El `package_hash` autorizado no coincide con el vivo (invariante 8:
    "el humano aprueba exactamente lo que vio")."""
