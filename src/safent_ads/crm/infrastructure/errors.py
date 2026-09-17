"""Errores de infraestructura de `crm`."""

from __future__ import annotations

from safent_ads.shared.errors import InfrastructureError


class CrmRequestError(InfrastructureError):
    """Fallo de red, timeout o respuesta no-2xx del endpoint del CRM.
    Nunca lleva el cuerpo crudo de la respuesta ni cabeceras de autorizacion."""


class MissingHashedIdentityError(InfrastructureError):
    """`lead_attributions.hashed_identity` es NOT NULL (0005_catalog_crm):
    una `LeadAttribution` sin identidad hasheada no se puede persistir tal
    cual -- el dominio la permite `None` (senal sin digest resuelto), la
    tabla no. Fallar aqui, en el borde de persistencia, en vez de guardar
    una fila que viole el invariante en silencio."""
