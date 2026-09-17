"""Cliente HTTP minimo que los adaptadores OAuth necesitan (Protocol +
doble de tests, mismo patron que `GoogleAdsSearchClient`/`MetaGraphClient`:
"SDK mocked in tests", T025/T026). La implementacion real
(`HttpxOAuthHttpClient`) pasa siempre por `egress_guard.build_guarded_async_client`
(threat-model.md C-12): el broker no habla HTTP con nadie fuera de la
lista blanca, ni siquiera para canjear un `code` OAuth."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from typing import Any, Protocol

import httpx

from safent_ads.broker.infrastructure.egress_guard import build_guarded_async_client
from safent_ads.shared.errors import InfrastructureError

_TIMEOUT_SECONDS = 10.0
_MAX_ERROR_BODY_BYTES = 64 * 1024


class OAuthHttpError(InfrastructureError):
    """Fallo de red o respuesta no-2xx al hablar con el proveedor OAuth,
    ya saneado (nunca el cuerpo crudo de la respuesta, threat-model.md
    C-13: puede llevar tokens)."""


class GoogleCloudProjectAccessError(OAuthHttpError):
    """El proyecto OAuth solo tiene acceso de prueba a Google Ads."""


class OAuthHttpClient(Protocol):
    async def post_form(self, url: str, *, data: Mapping[str, str]) -> Mapping[str, Any]: ...

    async def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]: ...

    async def post_json(
        self,
        url: str,
        *,
        json_body: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]: ...


class HttpxOAuthHttpClient:
    """Un `httpx.AsyncClient` egress-guardado por llamada: el broker no
    mantiene sesiones HTTP persistentes entre operaciones OAuth, que son
    poco frecuentes por diseno (conectar/reconectar, no un bucle)."""

    async def post_form(self, url: str, *, data: Mapping[str, str]) -> Mapping[str, Any]:
        async with build_guarded_async_client(timeout=_TIMEOUT_SECONDS) as client:
            response = await self._send(client.post(url, data=dict(data)))
        return _parse_json(response)

    async def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        async with build_guarded_async_client(timeout=_TIMEOUT_SECONDS) as client:
            response = await self._send(
                client.get(url, headers=dict(headers or {}), params=dict(params or {}))
            )
        return _parse_json(response)

    async def post_json(
        self,
        url: str,
        *,
        json_body: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        async with build_guarded_async_client(timeout=_TIMEOUT_SECONDS) as client:
            response = await self._send(
                client.post(url, json=dict(json_body), headers=dict(headers or {}))
            )
        return _parse_json(response)

    async def _send(self, pending: Awaitable[httpx.Response]) -> httpx.Response:
        try:
            response = await pending
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if _google_project_access_is_test_only(exc.response):
                raise GoogleCloudProjectAccessError(
                    "Google Cloud project is not approved for production accounts"
                ) from None
            # An SDK exception may include an opaque body/token without a
            # recognizable label. Only the numeric status is diagnostic data.
            raise OAuthHttpError(f"OAuth provider HTTP {exc.response.status_code}") from None
        except httpx.HTTPError:
            raise OAuthHttpError("OAuth transport request failed") from None
        return response


def _parse_json(response: httpx.Response) -> Mapping[str, Any]:
    try:
        body: Mapping[str, Any] = response.json()
    except ValueError as exc:
        raise OAuthHttpError("respuesta del proveedor OAuth no es JSON valido") from exc
    if not isinstance(body, dict):
        raise OAuthHttpError("respuesta del proveedor OAuth no es un objeto JSON")
    return body


def _google_project_access_is_test_only(response: httpx.Response) -> bool:
    """Solo un código estructurado del endpoint oficial, nunca texto libre.

    No devolver ni registrar el cuerpo, que puede incluir información sensible.
    La API v25 distingue este error de otros rechazos de permisos.
    """
    if (
        response.request.url.host != "googleads.googleapis.com"
        or response.status_code != httpx.codes.FORBIDDEN
    ):
        return False
    if len(response.content) > _MAX_ERROR_BODY_BYTES:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    error = body.get("error") if isinstance(body, dict) else None
    details = error.get("details") if isinstance(error, dict) else None
    if not isinstance(details, list):
        return False
    for detail in details:
        errors = detail.get("errors") if isinstance(detail, dict) else None
        if not isinstance(errors, list):
            continue
        for item in errors:
            code = item.get("errorCode") if isinstance(item, dict) else None
            if isinstance(code, dict) and code.get("authorizationError") == (
                "CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION"
            ):
                return True
    return False
