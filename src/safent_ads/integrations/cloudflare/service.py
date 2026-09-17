"""`NullCloudflareService`/`LiveCloudflareService` implementan
`CloudflarePort` (Anadido del dueno, 14-sep). `build_cloudflare_service` es
la UNICA fabrica: `composition/app.py` la llama con el token ya
desenvuelto de `SecretStr` (o `None`) -- este modulo no conoce pydantic.

`LiveCloudflareService` hace CERO llamadas de red para una zona fuera de
`allowed_zones` (falla cerrado antes de construir ninguna peticion, F-4);
resuelve nombre de zona -> `zone_id` con una llamada (`GET /zones?name=`)
porque la API v4 exige el ID opaco para las operaciones de registro. Nunca
cachea esa resolucion entre llamadas: el volumen esperado (conector de
infraestructura, no un bucle de metricas) no lo justifica."""

from __future__ import annotations

from typing import Any

from safent_ads.integrations.cloudflare.client import CloudflareHttpClient
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


class NullCloudflareService:
    """Sin token (ni conexion guardada en el panel, ni `CLOUDFLARE_API_
    TOKEN` de respaldo): cada operacion devuelve el sentinel sin tocar la
    red -- nunca lanza, nunca construye un `CloudflareHttpClient`.
    `create_token_url` (lane 006-cloudflare-ui) por defecto apunta al
    perfil: sin conexion previa no hay `account_id` que ofrecer un enlace
    mas concreto."""

    def __init__(self, *, create_token_url: str | None = None) -> None:
        self._not_configured = CloudflareNotConfigured(
            create_token_url=create_token_url or build_create_token_url(None)
        )

    async def list_dns_zones(self) -> CloudflareNotConfigured:
        return self._not_configured

    async def list_dns_records(
        self,
        zone: str,  # noqa: ARG002 - forma exacta del puerto
        *,
        record_type: DnsRecordType | None,  # noqa: ARG002 - forma exacta del puerto
        name: str | None,  # noqa: ARG002 - forma exacta del puerto
    ) -> CloudflareNotConfigured:
        return self._not_configured

    async def upsert_dns_record(
        self,
        zone: str,  # noqa: ARG002 - forma exacta del puerto
        *,
        record_type: DnsRecordType,  # noqa: ARG002 - forma exacta del puerto
        name: str,  # noqa: ARG002 - forma exacta del puerto
        content: str,  # noqa: ARG002 - forma exacta del puerto
        ttl: int,  # noqa: ARG002 - forma exacta del puerto
        proxied: bool,  # noqa: ARG002 - forma exacta del puerto
        comment: str | None,  # noqa: ARG002 - forma exacta del puerto
    ) -> CloudflareNotConfigured:
        return self._not_configured

    async def delete_dns_record(
        self,
        zone: str,  # noqa: ARG002 - forma exacta del puerto
        record_id: str,  # noqa: ARG002 - forma exacta del puerto
    ) -> CloudflareNotConfigured:
        return self._not_configured


class LiveCloudflareService:
    def __init__(self, client: CloudflareHttpClient, *, allowed_zones: frozenset[str]) -> None:
        self._client = client
        self._allowed_zones = allowed_zones

    async def list_dns_zones(self) -> tuple[CloudflareZone, ...]:
        raw_zones = await self._client.list_zones()
        return tuple(
            _zone_from_raw(raw) for raw in raw_zones if self._is_allowed(str(raw.get("name", "")))
        )

    async def list_dns_records(
        self, zone: str, *, record_type: DnsRecordType | None, name: str | None
    ) -> tuple[CloudflareDnsRecord, ...]:
        zone_id = await self._resolve_zone_id(zone)
        raw_type = record_type.value if record_type is not None else None
        raw_records = await self._client.list_dns_records(zone_id, record_type=raw_type, name=name)
        return tuple(_record_from_raw(raw) for raw in raw_records)

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
    ) -> CloudflareDnsRecord:
        zone_id = await self._resolve_zone_id(zone)
        payload = _record_payload(
            record_type, name, content, ttl=ttl, proxied=proxied, comment=comment
        )
        existing = await self._client.list_dns_records(
            zone_id, record_type=record_type.value, name=name
        )
        raw = await self._create_or_update(zone_id, existing, payload)
        return _record_from_raw(raw)

    async def delete_dns_record(self, zone: str, record_id: str) -> CloudflareDnsRecordDeleted:
        zone_id = await self._resolve_zone_id(zone)
        await self._client.delete_dns_record(zone_id, record_id)
        return CloudflareDnsRecordDeleted(record_id=record_id, zone=zone)

    async def _create_or_update(
        self, zone_id: str, existing: list[dict[str, Any]], payload: dict[str, Any]
    ) -> dict[str, Any]:
        if len(existing) > 1:
            raise CloudflareRecordAmbiguousError(
                f"{len(existing)} registros existentes coinciden en zona+tipo+nombre: "
                "borra el que sobre con delete_dns_record antes de reintentar"
            )
        if len(existing) == 1:
            return await self._client.update_dns_record(zone_id, str(existing[0]["id"]), payload)
        return await self._client.create_dns_record(zone_id, payload)

    def _is_allowed(self, zone_name: str) -> bool:
        return not self._allowed_zones or zone_name in self._allowed_zones

    async def _resolve_zone_id(self, zone: str) -> str:
        if not self._is_allowed(zone):
            raise CloudflareZoneNotAllowedError(f"zona fuera de la lista permitida: {zone}")
        matches = await self._client.list_zones(name=zone)
        if not matches:
            raise CloudflareZoneNotFoundError(f"zona no encontrada en cloudflare: {zone}")
        return str(matches[0]["id"])


def _zone_from_raw(raw: dict[str, Any]) -> CloudflareZone:
    return CloudflareZone(
        zone_id=str(raw["id"]), name=str(raw["name"]), status=str(raw.get("status", ""))
    )


def _record_from_raw(raw: dict[str, Any]) -> CloudflareDnsRecord:
    return CloudflareDnsRecord(
        record_id=str(raw["id"]),
        zone_id=str(raw["zone_id"]),
        type=str(raw["type"]),
        name=str(raw["name"]),
        content=str(raw["content"]),
        ttl=int(raw["ttl"]),
        proxied=bool(raw.get("proxied", False)),
        comment=raw.get("comment"),
    )


def _record_payload(
    record_type: DnsRecordType,
    name: str,
    content: str,
    *,
    ttl: int,
    proxied: bool,
    comment: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": record_type.value,
        "name": name,
        "content": content,
        "ttl": ttl,
        "proxied": proxied,
    }
    if comment is not None:
        payload["comment"] = comment
    return payload


def build_cloudflare_service(
    api_token: str | None,
    allowed_zones: frozenset[str],
    *,
    create_token_url: str | None = None,
) -> CloudflarePort:
    if not api_token:
        return NullCloudflareService(create_token_url=create_token_url)
    return LiveCloudflareService(CloudflareHttpClient(api_token), allowed_zones=allowed_zones)
