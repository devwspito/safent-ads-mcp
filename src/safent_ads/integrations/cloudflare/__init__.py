"""Conector propio de Cloudflare (API v4): acceso, control y auditoria
sobre DNS -- nunca coaching (Anadido del dueno, 14-sep). Superficie
publica del paquete; `composition/app.py`/`mcp/presentation/cloudflare_
tools.py` importan solo de aqui, nunca de `.client`/`.service` directamente.

Lane 006-cloudflare-ui (owner decision, "crear una conexion con
Cloudflare pidiendo el token e indicando el enlace donde crearlo") anade
la conexion gestionada desde el panel: `DynamicCloudflareService` (el
`CloudflarePort` real que `composition/app.py` cablea, token guardado
primero, `CLOUDFLARE_API_TOKEN` de respaldo despues), los tres casos de
uso de `connection_service.py` y `build_cloudflare_connection_router`
(REST)."""

from __future__ import annotations

from safent_ads.integrations.cloudflare.client import (
    CloudflareAccountIdError,
    CloudflareApiError,
    CloudflareResponseTooLargeError,
    CloudflareTransportError,
)
from safent_ads.integrations.cloudflare.connection_port import (
    CloudflareConnectionRecord,
    CloudflareConnectionStatus,
    CloudflareConnectionStore,
    CloudflareTokenInvalidError,
)
from safent_ads.integrations.cloudflare.connection_service import (
    REQUIRED_PERMISSIONS,
    ConnectCloudflareToken,
    DisconnectCloudflareToken,
    DynamicCloudflareService,
    GetCloudflareConnectionStatus,
)
from safent_ads.integrations.cloudflare.connection_store import (
    RequestScopedCloudflareConnectionStore,
    SqlCloudflareConnectionStore,
    build_request_scoped_cloudflare_connection_store,
)
from safent_ads.integrations.cloudflare.port import (
    CloudflareDnsRecord,
    CloudflareDnsRecordDeleted,
    CloudflareNotConfigured,
    CloudflarePort,
    CloudflareRecordAmbiguousError,
    CloudflareZone,
    CloudflareZoneNotAllowedError,
    CloudflareZoneNotFoundError,
    DnsRecordType,
    build_create_token_url,
)
from safent_ads.integrations.cloudflare.rest import build_cloudflare_connection_router
from safent_ads.integrations.cloudflare.service import build_cloudflare_service

__all__ = [
    "REQUIRED_PERMISSIONS",
    "CloudflareAccountIdError",
    "CloudflareApiError",
    "CloudflareConnectionRecord",
    "CloudflareConnectionStatus",
    "CloudflareConnectionStore",
    "CloudflareDnsRecord",
    "CloudflareDnsRecordDeleted",
    "CloudflareNotConfigured",
    "CloudflarePort",
    "CloudflareRecordAmbiguousError",
    "CloudflareResponseTooLargeError",
    "CloudflareTokenInvalidError",
    "CloudflareTransportError",
    "CloudflareZone",
    "CloudflareZoneNotAllowedError",
    "CloudflareZoneNotFoundError",
    "ConnectCloudflareToken",
    "DisconnectCloudflareToken",
    "DnsRecordType",
    "DynamicCloudflareService",
    "GetCloudflareConnectionStatus",
    "RequestScopedCloudflareConnectionStore",
    "SqlCloudflareConnectionStore",
    "build_cloudflare_connection_router",
    "build_cloudflare_service",
    "build_create_token_url",
    "build_request_scoped_cloudflare_connection_store",
]
