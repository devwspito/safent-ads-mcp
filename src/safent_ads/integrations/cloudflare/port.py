"""Contrato de aplicacion del conector Cloudflare (Anadido del dueno,
14-sep: "MCP=acceso+control+auditoria, no coaching"). DTOs propios --
nunca el dict crudo de la API v4 de Cloudflare cruza esta frontera (plan.md
§8: mapear entre DTO y dominio explicitamente, nunca dejar fugar el tipo
del proveedor).

`CloudflareNotConfigured` es un Null Object explicito, no una bandera:
cada operacion del puerto devuelve `T | CloudflareNotConfigured` -- el
llamante (`mcp/presentation/cloudflare_tools.py`) lo distingue con
`isinstance`, sin que ninguna capa pregunte "esta configurado?" antes de
llamar (`service.NullCloudflareService` hace ese trabajo una unica vez)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from safent_ads.shared.errors import ApplicationError

# lane 006-cloudflare-ui (owner decision): "crear una conexion con
# Cloudflare pidiendo el token e indicando el enlace donde crearlo".
# Sin `account_id` conocido, el unico enlace que Cloudflare siempre
# resuelve es el del perfil -- nunca inventamos un `account_id`.
_PROFILE_TOKENS_URL = "https://dash.cloudflare.com/profile/api-tokens"


def build_create_token_url(account_id: str | None) -> str:
    """El enlace exacto donde el propietario crea un token nuevo:
    ambito de cuenta (`/<account_id>/api-tokens`) si ya conocemos la
    cuenta de una conexion anterior, perfil en caso contrario. Funcion
    pura -- la usan tanto `connection_service.py` como `mcp/presentation/
    cloudflare_tools.py`/`rest.py`, sin duplicar el patron."""
    if account_id is None:
        return _PROFILE_TOKENS_URL
    return f"https://dash.cloudflare.com/{account_id}/api-tokens"


class DnsRecordType(StrEnum):
    """Los cinco tipos que este conector administra (Anadido del dueno):
    ampliar la lista es una decision de producto, fuera de esta lane."""

    A = "A"
    AAAA = "AAAA"
    CNAME = "CNAME"
    TXT = "TXT"
    MX = "MX"


@dataclass(frozen=True, slots=True)
class CloudflareZone:
    zone_id: str
    name: str
    status: str


@dataclass(frozen=True, slots=True)
class CloudflareDnsRecord:
    record_id: str
    zone_id: str
    type: str
    name: str
    content: str
    ttl: int
    proxied: bool
    comment: str | None


@dataclass(frozen=True, slots=True)
class CloudflareDnsRecordDeleted:
    record_id: str
    zone: str


@dataclass(frozen=True, slots=True)
class CloudflareNotConfigured:
    """Sentinel: ni conexion guardada en el panel ni `CLOUDFLARE_API_TOKEN`
    de respaldo. Sigue sin ser una excepcion a este nivel (Null Object,
    Anadido del dueno) -- `mcp/presentation/cloudflare_tools.py` es quien
    decide traducirlo a `CLOUDFLARE_NOT_CONNECTED` (lane 006-cloudflare-ui:
    "indicando el enlace donde crearlo"). `create_token_url` viaja en el
    propio sentinel para que ese borde nunca tenga que volver a resolver
    el `account_id` -- quien construye el sentinel (`NullCloudflareService`/
    `DynamicCloudflareService`) ya lo conoce."""

    create_token_url: str = _PROFILE_TOKENS_URL


class CloudflareZoneNotAllowedError(ApplicationError):
    """La zona pedida no esta en `CLOUDFLARE_ALLOWED_ZONES` (cuando esta
    configurada) -- rechazo antes de tocar la red."""


class CloudflareZoneNotFoundError(ApplicationError):
    """La zona esta permitida pero no existe en la cuenta de Cloudflare del
    token configurado (desajuste de configuracion, no un fallo de
    Cloudflare)."""


class CloudflareRecordAmbiguousError(ApplicationError):
    """`upsert_dns_record` encontro mas de un registro existente con el
    mismo (zona, tipo, nombre): no hay un unico candidato al que aplicar el
    update, y machacar el primero podria borrar el registro equivocado."""


class CloudflarePort(Protocol):
    """Puerto que `mcp/presentation/cloudflare_tools.py` consume -- nunca
    conoce `CloudflareHttpClient` ni la forma de la API v4."""

    async def list_dns_zones(self) -> tuple[CloudflareZone, ...] | CloudflareNotConfigured: ...

    async def list_dns_records(
        self, zone: str, *, record_type: DnsRecordType | None, name: str | None
    ) -> tuple[CloudflareDnsRecord, ...] | CloudflareNotConfigured: ...

    async def upsert_dns_record(
        self,
        zone: str,
        *,
        record_type: DnsRecordType,
        name: str,
        content: str,
        ttl: int,
        proxied: bool,
        comment: str | None,
    ) -> CloudflareDnsRecord | CloudflareNotConfigured: ...

    async def delete_dns_record(
        self, zone: str, record_id: str
    ) -> CloudflareDnsRecordDeleted | CloudflareNotConfigured: ...
