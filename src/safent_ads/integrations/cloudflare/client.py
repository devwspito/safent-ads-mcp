"""`CloudflareHttpClient`: adaptador delgado sobre la API v4 de Cloudflare
(`https://api.cloudflare.com/client/v4`). Anadido del dueno (14-sep):
"MCP=acceso+control+auditoria, no coaching" -- el arnes no puede llegar a
Cloudflare sin la credencial de la empresa; este es el conector propio que
la sostiene (nunca el SDK oficial, no sancionado por el arquitecto).

Host UNICO y fijo (`CLOUDFLARE_API_HOST`, nunca un parametro que llegue de
fuera): la unica variable de la URL es la ruta (`/zones`, `/zones/{id}/
dns_records`, ...), construida siempre con IDs ya validados por
`integrations/cloudflare/service.py` -- no hace falta una lista blanca de
host por peticion, a diferencia de `broker/infrastructure/egress_guard.py`
(multiples hosts). Mismo fijado de conexion (F-3/F-4, CWE-367: DNS
rebinding) que `creative/infrastructure/http_asset_fetcher.py` sobre
`shared/net/safe_egress.py` -- sin importar de `broker` (bounded context
distinto, plan.md §4: modulos de un solo sentido).

`trust_env=False`, sin redirecciones, timeout de 5 s y tope de 64 KiB de
cuerpo (streaming: corta en cuanto se detecta, nunca `len(response.
content)` al final). Ningun error propaga el token ni el cuerpo crudo de
Cloudflare -- `CloudflareApiError` solo lleva el `status_code`."""

from __future__ import annotations

import json
import re
from typing import Any, Final

import httpx

from safent_ads.shared.errors import InfrastructureError
from safent_ads.shared.net.safe_egress import (
    BlockedEgressAddressError,
    Resolver,
    default_resolver,
    pin_request,
)

CLOUDFLARE_API_HOST: Final = "api.cloudflare.com"
_BASE_URL: Final = f"https://{CLOUDFLARE_API_HOST}/client/v4"
_TIMEOUT_SECONDS: Final = 5.0
_MAX_RESPONSE_BYTES: Final = 64 * 1024
_ZONES_PAGE_SIZE = "50"
_RECORDS_PAGE_SIZE = "100"
_ACCOUNTS_PAGE_SIZE = "50"
_HTTP_ERROR_STATUS: Final = 400
# Cloudflare emite IDs de cuenta como hex de 32 caracteres en minuscula
# (lane 006-cloudflare-ui): validado ANTES de interpolarlo en la ruta --
# nunca deja que un `account_id` no fiable entre en la URL (F-3, mismo
# criterio que `_session_path` de `broker/infrastructure/credential_store.py`).
_ACCOUNT_ID_PATTERN: Final = re.compile(r"^[0-9a-f]{32}$")


class CloudflareAccountIdError(InfrastructureError):
    """`account_id` no tiene la forma hexadecimal de 32 caracteres que
    Cloudflare emite -- rechazo antes de construir la peticion."""


class CloudflareTransportError(InfrastructureError):
    """Fallo de red, DNS o host bloqueado hablando con Cloudflare. El
    mensaje es siempre propio (nunca incluye el token ni un cuerpo de
    respuesta)."""


class CloudflareResponseTooLargeError(InfrastructureError):
    """El cuerpo de la respuesta supera `_MAX_RESPONSE_BYTES` -- se corta
    en cuanto se detecta, igual que `AssetFetchTooLargeError`."""


class CloudflareApiError(InfrastructureError):
    """Cloudflare respondio con estado >= 400 o `success: false`.
    `status_code` es la UNICA senal que se propaga -- nunca `errors`/
    `messages` del cuerpo (pueden llevar metadatos de la cuenta)."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"cloudflare respondio con estado {status_code}")
        self.status_code = status_code


class CloudflareHttpClient:
    """Un token, un host, cinco operaciones (zonas + CRUD de registros
    DNS). `transport`/`resolver` inyectables para tests deterministas,
    mismo criterio que `HttpAssetFetcher`."""

    def __init__(
        self,
        api_token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
        timeout_s: float = _TIMEOUT_SECONDS,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            headers={"Authorization": f"Bearer {api_token}"},
        )
        self._resolver = resolver or default_resolver
        self._timeout_s = timeout_s
        self._max_response_bytes = max_response_bytes

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_zones(self, *, name: str | None = None) -> list[dict[str, Any]]:
        params = {"per_page": _ZONES_PAGE_SIZE}
        if name is not None:
            params["name"] = name
        return _expect_list(await self._send("GET", "/zones", params=params))

    async def verify_user_token(self) -> dict[str, Any]:
        """`GET /user/tokens/verify` (lane 006-cloudflare-ui): valida un
        token de USUARIO antes de guardar nada -- lanza `CloudflareApiError`
        si Cloudflare lo rechaza, nunca devuelve un resultado a medias."""
        return _expect_dict(await self._send("GET", "/user/tokens/verify"))

    async def verify_account_token(self, account_id: str) -> dict[str, Any]:
        """`GET /accounts/{id}/tokens/verify`: valida un token de CUENTA
        (Account Owned Token). Mismo contrato que `verify_user_token`."""
        path = f"/accounts/{_validated_account_id(account_id)}/tokens/verify"
        return _expect_dict(await self._send("GET", path))

    async def list_accounts(self) -> list[dict[str, Any]]:
        """`GET /accounts`: solo para auto-descubrir `account_id` cuando el
        propietario no lo escribio -- best effort, un token sin permiso de
        cuenta puede rechazar esto sin que la conexion falle por ello
        (`connection_service.py::ConnectCloudflareToken`)."""
        params = {"per_page": _ACCOUNTS_PAGE_SIZE}
        return _expect_list(await self._send("GET", "/accounts", params=params))

    async def list_dns_records(
        self, zone_id: str, *, record_type: str | None, name: str | None
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {"per_page": _RECORDS_PAGE_SIZE}
        if record_type is not None:
            params["type"] = record_type
        if name is not None:
            params["name"] = name
        path = f"/zones/{zone_id}/dns_records"
        return _expect_list(await self._send("GET", path, params=params))

    async def create_dns_record(self, zone_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = await self._send("POST", f"/zones/{zone_id}/dns_records", json_body=payload)
        return _expect_dict(result)

    async def update_dns_record(
        self, zone_id: str, record_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        path = f"/zones/{zone_id}/dns_records/{record_id}"
        return _expect_dict(await self._send("PUT", path, json_body=payload))

    async def delete_dns_record(self, zone_id: str, record_id: str) -> dict[str, Any]:
        path = f"/zones/{zone_id}/dns_records/{record_id}"
        return _expect_dict(await self._send("DELETE", path))

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:  # noqa: ANN401 - la forma exacta depende del endpoint
        request = self._client.build_request(
            method, path, params=params, json=json_body, timeout=self._timeout_s
        )
        await self._pin(request)
        body, status_code = await self._read_within_limit(request)
        return _unwrap_envelope(status_code, body)

    async def _pin(self, request: httpx.Request) -> None:
        try:
            await pin_request(request, resolver=self._resolver)
        except BlockedEgressAddressError as exc:
            raise CloudflareTransportError("no se pudo validar el host de cloudflare") from exc

    async def _read_within_limit(self, request: httpx.Request) -> tuple[bytes, int]:
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise CloudflareTransportError("cloudflare no respondio") from exc
        try:
            return await self._drain(response), response.status_code
        finally:
            await response.aclose()

    async def _drain(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > self._max_response_bytes:
                raise CloudflareResponseTooLargeError(f"{total} bytes > {self._max_response_bytes}")
            chunks.append(chunk)
        return b"".join(chunks)


def _validated_account_id(account_id: str) -> str:
    if not _ACCOUNT_ID_PATTERN.fullmatch(account_id):
        raise CloudflareAccountIdError("account_id no tiene forma hexadecimal valida")
    return account_id


def _unwrap_envelope(status_code: int, body: bytes) -> Any:  # noqa: ANN401
    try:
        payload = json.loads(body) if body else {}
    except ValueError as exc:
        raise CloudflareTransportError("cloudflare devolvio un cuerpo no valido") from exc
    if status_code >= _HTTP_ERROR_STATUS or not payload.get("success", False):
        raise CloudflareApiError(status_code)
    return payload.get("result")


def _expect_list(value: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    if not isinstance(value, list):
        raise CloudflareTransportError("cloudflare devolvio una forma inesperada")
    return value


def _expect_dict(value: Any) -> dict[str, Any]:  # noqa: ANN401
    if not isinstance(value, dict):
        raise CloudflareTransportError("cloudflare devolvio una forma inesperada")
    return value
