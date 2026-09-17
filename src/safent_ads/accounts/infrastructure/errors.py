"""Excepciones de infraestructura de `accounts` (shared/errors.py: "Fallo de
un adaptador: base de datos, socket del broker, SDK externo")."""

from __future__ import annotations

from safent_ads.shared.errors import InfrastructureError


class BrokerConnectionError(InfrastructureError):
    """No se pudo conectar con `$ADS_BROKER_SOCKET` a tiempo."""
