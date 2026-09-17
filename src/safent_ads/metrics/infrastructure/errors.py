"""Excepciones de infraestructura de `metrics`."""

from __future__ import annotations

from safent_ads.shared.errors import InfrastructureError


class UnknownEntityRefError(InfrastructureError):
    """El hecho o la correccion apuntan a una entidad que no esta en
    `ad_entities`. El esquema exige el negocio y la cuenta de esa entidad
    (FK compuesta), asi que ingerir a ciegas dejaria metricas sin dueno."""


class UnknownAccountRefError(InfrastructureError):
    """`Freshness.platform_account_ref` apunta a una cuenta que no esta en
    `platform_accounts`: `data_freshness.platform_account_id` es NOT NULL
    con FK, asi que guardar a ciegas dejaria la fila sin dueno."""
