"""Errores tipados de `opportunities` (tasks.md T114): `mcp/presentation`
los traduce a los codigos del contrato (`ENTITY_NOT_FOUND`/
`GUARDRAIL_BLOCKED`, contracts/mcp-tools.md regla 6)."""

from __future__ import annotations

from safent_ads.shared.errors import ApplicationError


class OfferingNotFoundError(ApplicationError):
    """`offering_id` no existe o no esta activo para este negocio."""


class NoActiveAccountForPlatformError(ApplicationError):
    """El negocio no tiene ninguna cuenta `ACTIVE` en esa plataforma."""


class AmbiguousActiveAccountForPlatformError(ApplicationError):
    """El negocio tiene mas de una cuenta `ACTIVE` en esa plataforma (varias
    conexiones, o varias cuentas remotas bajo la misma conexion): elegir la
    primera en silencio arriesgaria proponer sobre la cuenta equivocada.
    Sin una entrada explicita que desambigue, se rechaza."""


class DailyBudgetExceedsCapError(ApplicationError):
    """`daily_budget` supera el `daily_cap` ya configurado para la cuenta
    (guardarrail de ambito `platform_account`)."""


class ChannelTypeNotEnabledError(ApplicationError):
    """tasks.md T076 (POLISH): el canal nativo de Google del brief es una
    fila valida de `GoogleAdvertisingChannelType`, pero esta instalacion no
    lo tiene encendido (`ADS_GOOGLE_CHANNELS_ENABLED`, settings.py).
    Defensa en profundidad de `ProposeCampaign.execute` -- el borrador
    (`mcp.presentation.campaign_draft_tools`) ya lo exige al guardar, pero
    un borrador puede quedarse dias entre guardar y promover, y la
    configuracion de la instalacion puede cambiar en ese tiempo."""

    def __init__(self, *, channel: str) -> None:
        super().__init__(channel)
        self.channel = channel
