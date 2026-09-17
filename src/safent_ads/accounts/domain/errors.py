"""Excepciones de dominio propias de `accounts` (shared/errors.py: "Cada
bounded context define sus propias excepciones concretas heredando de estas
bases; la presentacion mapea por tipo, nunca por mensaje")."""

from __future__ import annotations

from safent_ads.shared.errors import DomainError


class InvalidStateTransitionError(DomainError):
    """Transicion de estado no permitida para el agregado (p.ej. `PlatformAccount`
    en `SUSPENDED` intentando pasar a `THROTTLED` sin reconciliacion manual)."""


class InvalidEntityHierarchyError(DomainError):
    """`ParentRef` no corresponde al nivel inmediatamente superior de `AdEntity`
    (data-model.md: "parent_id apunta exactamente al nivel inmediatamente
    superior")."""


class EntityNotControllableError(DomainError):
    """Intento de mutar una palanca que la plataforma no expone
    (`is_controllable = false`, FR-41: Advantage+/PMax admiten lectura y
    presupuesto, nada mas)."""


class EntityDriftedError(DomainError):
    """La entidad esta `drifted`: no admite mutaciones hasta reconciliar
    (data-model.md invariante de `AdEntity`)."""


class AccountOwnershipConflictError(DomainError):
    """An existing account reference cannot be assigned to a different business."""
