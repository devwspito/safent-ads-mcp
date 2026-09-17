"""Jerarquia de errores base por capa (plan.md §8: "excepciones de dominio ->
mapeador en presentacion. Sin stack traces ni secretos hacia el usuario.").

Cada bounded context define sus propias excepciones concretas heredando de
estas bases; la presentacion mapea por tipo, nunca por mensaje."""

from __future__ import annotations


class SafentAdsError(Exception):
    """Raiz de toda excepcion propia de safent-ads. Nunca se lanza directamente."""


class DomainError(SafentAdsError):
    """Violacion de un invariante de un agregado o value object."""


class ApplicationError(SafentAdsError):
    """Fallo de un caso de uso: entidad no encontrada, precondicion de puerto, etc."""


class InfrastructureError(SafentAdsError):
    """Fallo de un adaptador: base de datos, socket del broker, SDK externo."""
