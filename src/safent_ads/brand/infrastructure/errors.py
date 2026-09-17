"""Errores de infraestructura de `brand`."""

from __future__ import annotations

from safent_ads.shared.errors import InfrastructureError


class BrandKitYamlError(InfrastructureError):
    """`config/brand/<business>.yaml` ausente, con YAML invalido, con un
    esquema incorrecto, o que produce un `BrandKit` que viola un
    invariante de dominio (p.ej. `forbidden_claims` manipulado a mano por
    debajo del suelo de seguridad)."""
