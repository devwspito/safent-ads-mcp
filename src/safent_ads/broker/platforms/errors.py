"""Excepciones de infraestructura propias de `broker/platforms`
(shared/errors.py: "Fallo de un adaptador: base de datos, socket del broker,
SDK externo")."""

from __future__ import annotations

from safent_ads.shared.errors import InfrastructureError


class GaqlValidationError(InfrastructureError):
    """`run_gaql` rechazo la consulta: no es `SELECT`, tiene mas de una
    sentencia, usa una palabra prohibida o un recurso fuera de la lista
    blanca (contracts/platform-port.md)."""


class PlatformCapabilityNotImplementedError(InfrastructureError):
    """Metodo del puerto todavia no implementado en esta fase (p.ej.
    `upload_asset`, reservado para el contexto `creative`)."""


class DailyOperationBudgetExhaustedError(InfrastructureError):
    """Tope diario de operaciones del tier de la API agotado
    (research/ads-platforms-and-mcps.md §3: Explorer 2.880/dia)."""


class WriteBudgetWindowExhaustedError(InfrastructureError):
    """Tope de la ventana deslizante de escrituras agotado (Meta Limited
    ~20/5 min)."""


class CredentialNotConnectedError(InfrastructureError):
    """`CredentialStorePort.get_credential` no tiene ninguna credencial de
    CLIENTE para esa cuenta externa (integracion: OAuth desde el panel,
    lane `us3-oauth-connect`). Fail closed -- nunca se completa con un
    token de otra cuenta ni con las credenciales de VENDOR."""


class GoogleAssetUploadError(InfrastructureError):
    """`AssetService.mutate_assets` (Google Ads v25) fallo al crear el
    `ImageAsset` (`LiveGoogleAssetUploadClient.mutate_image_asset`).
    Mensaje ya saneado por `redact_sdk_error` -- nunca el error crudo del
    SDK cruza esta frontera."""
