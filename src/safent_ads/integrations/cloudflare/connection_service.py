"""Casos de uso de la conexion Cloudflare (lane 006-cloudflare-ui, owner
decision): `GetCloudflareConnectionStatus`/`ConnectCloudflareToken`/
`DisconnectCloudflareToken` sobre `CloudflareConnectionStore`, mas
`DynamicCloudflareService` -- el `CloudflarePort` que
`composition/app.py` cablea de verdad para las 4 herramientas de DNS.

`DynamicCloudflareService` resuelve el token en CADA llamada (nunca uno
fijado al arrancar el proceso, a diferencia de `build_cloudflare_service`
de `service.py`): token guardado por el panel primero, `CLOUDFLARE_API_
TOKEN` de respaldo despues -- conectar desde el panel tiene que
funcionar sin reiniciar `ads-api` (spec del encargo). Construye un
`CloudflareHttpClient` nuevo por llamada y lo cierra siempre: mismo
criterio de simplicidad que `platform_apps_router.py` (conector propio,
volumen bajo, sin pool que mantener vivo entre peticiones)."""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from safent_ads.integrations.cloudflare.client import (
    CloudflareApiError,
    CloudflareHttpClient,
    CloudflareResponseTooLargeError,
    CloudflareTransportError,
)
from safent_ads.integrations.cloudflare.connection_port import (
    CloudflareConnectionRecord,
    CloudflareConnectionStatus,
    CloudflareConnectionStore,
    CloudflareTokenInvalidError,
)
from safent_ads.integrations.cloudflare.port import (
    CloudflareDnsRecord,
    CloudflareDnsRecordDeleted,
    CloudflareNotConfigured,
    CloudflareZone,
    DnsRecordType,
    build_create_token_url,
)
from safent_ads.integrations.cloudflare.service import LiveCloudflareService
from safent_ads.shared.clock import Clock

# `CloudflareHttpClient` es el valor por defecto real en produccion;
# inyectable para que los tests construyan uno con `transport=httpx.
# MockTransport(...)` sin tocar la red, mismo patron que `test_client.py`.
ClientFactory = Callable[[str], CloudflareHttpClient]

# Permisos minimos que el panel pide crear (owner decision): lectura de
# zona + edicion de DNS -- las cuatro herramientas MCP nunca hacen nada
# fuera de ese alcance (list/upsert/delete de registros DNS, list de
# zonas). Constante compartida por `rest.py` y `mcp/presentation/
# cloudflare_tools.py`, nunca duplicada como literal.
REQUIRED_PERMISSIONS: tuple[str, ...] = ("Zone.Read", "DNS.Edit")

# Cloudflare emite IDs de cuenta como hex de 32 caracteres en minuscula --
# mismo patron que `client.py::_ACCOUNT_ID_PATTERN`/`rest.py::
# _ACCOUNT_ID_PATTERN`, copia local a proposito (`_discover_account_id`
# es la unica ruta donde un `id` de la RESPUESTA de Cloudflare, no del
# propietario, llega a guardarse sin pasar por la validacion del borde
# HTTP).
_ACCOUNT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

_INVALID_CREDENTIAL_MESSAGE = (
    "Cloudflare rechazo el token o no puede leer zonas DNS con el. "
    "Revisa los permisos (Zone.Read, DNS.Edit) y vuelve a intentarlo."
)


def _account_id_from_zones(zones: list[dict[str, Any]]) -> str | None:
    """Cloudflare incluye `account: {id, name}` en cada zona de `GET
    /zones` -- unica forma de descubrir la cuenta de un token de CUENTA
    (`cfat_...`) que no puede llamar a `GET /accounts` (ese endpoint es
    de token de usuario). Mismo patron de validacion que un `account_id`
    tecleado por el propietario: nunca confiar en un id ajeno sin
    validarlo primero."""
    for zone in zones:
        account = zone.get("account")
        if not isinstance(account, dict):
            continue
        account_id = account.get("id")
        if account_id and _ACCOUNT_ID_PATTERN.fullmatch(str(account_id)):
            return str(account_id)
    return None


class GetCloudflareConnectionStatus:
    def __init__(
        self, store: CloudflareConnectionStore, *, fallback_token_configured: bool
    ) -> None:
        self._store = store
        self._fallback_token_configured = fallback_token_configured

    async def execute(self) -> CloudflareConnectionStatus:
        record = await self._store.get()
        if record is not None:
            return CloudflareConnectionStatus(
                connected=True,
                account_id=record.account_id,
                zones=record.zones,
                connected_at=record.connected_at,
                create_token_url=build_create_token_url(record.account_id),
                required_permissions=REQUIRED_PERMISSIONS,
            )
        return CloudflareConnectionStatus(
            connected=self._fallback_token_configured,
            account_id=None,
            zones=(),
            connected_at=None,
            create_token_url=build_create_token_url(None),
            required_permissions=REQUIRED_PERMISSIONS,
        )


class ConnectCloudflareToken:
    def __init__(
        self,
        store: CloudflareConnectionStore,
        *,
        clock: Clock,
        client_factory: ClientFactory = CloudflareHttpClient,
    ) -> None:
        self._store = store
        self._clock = clock
        self._client_factory = client_factory

    async def execute(
        self, *, token: str, account_id: str | None, owner_id: uuid.UUID
    ) -> CloudflareConnectionStatus:
        resolved_account_id, zones = await self._verify_and_discover(token, account_id)
        connected_at = self._clock.now()
        await self._store.save(
            CloudflareConnectionRecord(
                token=token,
                account_id=resolved_account_id,
                zones=zones,
                connected_at=connected_at,
                connected_by_owner_id=owner_id,
            )
        )
        return CloudflareConnectionStatus(
            connected=True,
            account_id=resolved_account_id,
            zones=zones,
            connected_at=connected_at,
            create_token_url=build_create_token_url(resolved_account_id),
            required_permissions=REQUIRED_PERMISSIONS,
        )

    async def _verify_and_discover(
        self, token: str, account_id: str | None
    ) -> tuple[str | None, tuple[str, ...]]:
        """Valida el token contra Cloudflare (regla del encargo: "valida
        contra la API primero") y descubre las zonas que puede leer. Nunca
        guarda nada aqui -- solo `execute` persiste, y solo si esto no
        lanza."""
        client = self._client_factory(token)
        try:
            if account_id is not None:
                await client.verify_account_token(account_id)
                raw_zones = await client.list_zones()
                resolved_account_id: str | None = account_id
            else:
                raw_zones, resolved_account_id = await self._verify_without_account_id(client)
            zones = tuple(str(zone["name"]) for zone in raw_zones)
            if resolved_account_id is None:
                resolved_account_id = await self._discover_account_id(client)
            return resolved_account_id, zones
        except (
            CloudflareApiError,
            CloudflareTransportError,
            CloudflareResponseTooLargeError,
        ) as exc:
            raise CloudflareTokenInvalidError(_INVALID_CREDENTIAL_MESSAGE) from exc
        finally:
            await client.aclose()

    async def _verify_without_account_id(
        self, client: CloudflareHttpClient
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Sin `account_id` tecleado (hallazgo produccion 16-sep):
        Cloudflare emite ahora tokens de CUENTA (`cfat_...`) que `GET
        /user/tokens/verify` rechaza (`{code: 1000, message: "Invalid API
        Token"}`) aunque el mismo token sea valido para `GET /accounts/
        {id}/tokens/verify` -- el propietario que pega un token de cuenta
        sin teclear el id de cuenta se quedaba con `CLOUDFLARE_TOKEN_
        INVALID`. Si el token de usuario no verifica, se prueba como
        token de cuenta antes de rechazarlo; si si verifica, sigue el
        camino de siempre (token de usuario, cuenta a descubrir aparte)."""
        try:
            await client.verify_user_token()
        except CloudflareApiError:
            return await self._verify_as_account_token(client)
        return await client.list_zones(), None

    async def _verify_as_account_token(
        self, client: CloudflareHttpClient
    ) -> tuple[list[dict[str, Any]], str]:
        """Fail closed: nunca acepta un token que no pueda listar ninguna
        zona. Deriva el `account_id` de `zone['account']['id']` --
        Cloudflare lo incluye en cada zona -- lo valida con el mismo
        patron que un `account_id` tecleado (`_ACCOUNT_ID_PATTERN`, nunca
        confiar en un id ajeno sin validar) y solo acepta el token si
        `GET /accounts/{id}/tokens/verify` en ESA cuenta responde bien."""
        raw_zones = await client.list_zones()
        derived_account_id = _account_id_from_zones(raw_zones)
        if derived_account_id is None:
            raise CloudflareTokenInvalidError(_INVALID_CREDENTIAL_MESSAGE)
        await client.verify_account_token(derived_account_id)
        return raw_zones, derived_account_id

    async def _discover_account_id(self, client: CloudflareHttpClient) -> str | None:
        """Mejor esfuerzo (regla del encargo: "cuando sea descubrible"): un
        token sin `Account Settings Read` rechaza `GET /accounts` sin que
        eso tumbe la conexion -- se queda sin `account_id`, el enlace de
        creacion de token cae al de perfil (`build_create_token_url(None)`).

        `_ACCOUNT_ID_PATTERN` (revision de seguridad de la conexion
        Cloudflare, 2026-09-15, hallazgo info): `rest.py` valida esta
        misma forma cuando el propietario TECLEA el `account_id`, pero un
        `id` descubierto via `GET /accounts` (respuesta de Cloudflare, no
        del propietario) se guardaba sin pasar por esa guardia -- fail
        closed aqui tambien, igual criterio que `client.py::
        _ACCOUNT_ID_PATTERN`."""
        try:
            accounts = await client.list_accounts()
        except (CloudflareApiError, CloudflareTransportError, CloudflareResponseTooLargeError):
            return None
        if len(accounts) != 1:
            return None
        account_id = accounts[0].get("id")
        if not account_id or not _ACCOUNT_ID_PATTERN.fullmatch(str(account_id)):
            return None
        return str(account_id)


class DisconnectCloudflareToken:
    def __init__(self, store: CloudflareConnectionStore) -> None:
        self._store = store

    async def execute(self) -> None:
        await self._store.delete()


@dataclass(frozen=True, slots=True)
class DynamicCloudflareService:
    """`CloudflarePort` real que usan las 4 herramientas de DNS
    (`mcp/presentation/cloudflare_tools.py`): resuelve el token guardado
    en el panel primero, `CLOUDFLARE_API_TOKEN` de respaldo despues, en
    CADA llamada -- nunca uno fijado al arrancar el proceso."""

    store: CloudflareConnectionStore
    fallback_token: str | None
    allowed_zones: frozenset[str]
    client_factory: ClientFactory = CloudflareHttpClient

    async def list_dns_zones(self) -> tuple[CloudflareZone, ...] | CloudflareNotConfigured:
        return await self._delegate(lambda live: live.list_dns_zones())

    async def list_dns_records(
        self, zone: str, *, record_type: DnsRecordType | None, name: str | None
    ) -> tuple[CloudflareDnsRecord, ...] | CloudflareNotConfigured:
        return await self._delegate(
            lambda live: live.list_dns_records(zone, record_type=record_type, name=name)
        )

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
    ) -> CloudflareDnsRecord | CloudflareNotConfigured:
        return await self._delegate(
            lambda live: live.upsert_dns_record(
                zone,
                record_type=record_type,
                name=name,
                content=content,
                ttl=ttl,
                proxied=proxied,
                comment=comment,
            )
        )

    async def delete_dns_record(
        self, zone: str, record_id: str
    ) -> CloudflareDnsRecordDeleted | CloudflareNotConfigured:
        return await self._delegate(lambda live: live.delete_dns_record(zone, record_id))

    async def _delegate[T](
        self, call: Callable[[LiveCloudflareService], Awaitable[T]]
    ) -> T | CloudflareNotConfigured:
        record = await self.store.get()
        if record is not None:
            client = self.client_factory(record.token)
            try:
                live = LiveCloudflareService(client, allowed_zones=self.allowed_zones)
                return await call(live)
            finally:
                await client.aclose()
        if self.fallback_token:
            client = self.client_factory(self.fallback_token)
            try:
                live = LiveCloudflareService(client, allowed_zones=self.allowed_zones)
                return await call(live)
            finally:
                await client.aclose()
        return CloudflareNotConfigured(create_token_url=build_create_token_url(None))
