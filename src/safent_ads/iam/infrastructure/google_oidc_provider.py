"""Adaptador del proveedor de identidad de Google (research.md Decision D):
construye la URL de autorizacion y canjea el `code` por el `id_token`. Solo
transporte -- quien decide si unas claims valen es
`iam/application/federated_id_token.py`, funcion pura y probable sin red.

La firma del `id_token` NO se re-verifica contra el JWKS de Google: llega por
el canal TLS directo con el token endpoint en respuesta a NUESTRO canje, que
es el caso que OIDC Core 3.1.3.7 permite apoyar en el TLS del canal. Hacerlo
anadiria un segundo host de egreso, una cache de claves y su invalidacion.

Egreso: un unico host fijo (`oauth2.googleapis.com`) a traves del guard
compartido `shared/net/safe_egress`, sin copiar ni una linea de bloqueo y sin
`httpx.AsyncClient` desnudo. Ningun error lleva el cuerpo crudo de la
respuesta ni la URL: el cuerpo puede traer tokens (FR-118)."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlencode

import httpx

from safent_ads.iam.application.federated_id_token import (
    FederatedIdTokenInvalidError,
    validate_id_token_claims,
)
from safent_ads.iam.application.ports import FederatedIdentityClaims
from safent_ads.iam.domain.federated_transaction import ReferenceHash
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import InfrastructureError
from safent_ads.shared.net.safe_egress import (
    BlockedEgressAddressError,
    Resolver,
    build_pinned_async_client,
    default_resolver,
)

_AUTHORIZATION_ENDPOINT: Final = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT: Final = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint, no secreto
# Derivado del endpoint para que lista blanca y destino no puedan divergir.
_ALLOWED_TOKEN_HOSTS: Final = frozenset({httpx.URL(_TOKEN_ENDPOINT).host})
_SCOPE: Final = "openid email"
_TIMEOUT_SECONDS: Final = 10.0
_JWT_SEGMENTS: Final = 3


class GoogleOidcError(InfrastructureError):
    """Fallo del tramo contra Google, ya saneado: nunca un token, un cuerpo
    crudo ni una URL en el mensaje."""


@dataclass(frozen=True, slots=True)
class GoogleOidcConfig:
    client_id: str
    client_secret: str = field(repr=False)


class GoogleOidcProvider:
    """Implementa el puerto `FederatedIdentityProvider` de
    `iam/application/ports.py` (plan.md, Application): `authorization_url` y
    `exchange_code`, nada mas."""

    def __init__(
        self,
        config: GoogleOidcConfig,
        clock: Clock,
        *,
        resolver: Resolver | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._resolver = resolver or default_resolver

    def authorization_url(self, *, state: str, nonce: str, redirect_uri: str) -> str:
        params = {
            "client_id": self._config.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": _SCOPE,
            "state": state,
            "nonce": nonce,
            # El dueno puede tener varias cuentas de Google abiertas en el
            # navegador: que elija con cual entra en vez de asumir la activa.
            "prompt": "select_account",
        }
        return f"{_AUTHORIZATION_ENDPOINT}?{urlencode(params)}"

    async def exchange_code(
        self, *, code: str, redirect_uri: str, expected_nonce_hash: ReferenceHash
    ) -> FederatedIdentityClaims:
        body = await self._post_token(code=code, redirect_uri=redirect_uri)
        id_token = body.get("id_token")
        if not isinstance(id_token, str):
            raise GoogleOidcError("Google no devolvio id_token")
        try:
            return validate_id_token_claims(
                _decode_jwt_payload_unverified(id_token),
                expected_audience=self._config.client_id,
                expected_nonce_hash=expected_nonce_hash,
                now=self._clock.now(),
            )
        except FederatedIdTokenInvalidError as exc:
            # Se traduce al error de infraestructura de este adaptador: la
            # presentacion solo conoce `GoogleOidcError` (mapeado a
            # `federated_error=provider_unavailable`), nunca el tipo interno
            # de `federated_id_token.py`.
            raise GoogleOidcError("id_token de Google invalido") from exc

    async def _post_token(self, *, code: str, redirect_uri: str) -> dict[str, Any]:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "redirect_uri": redirect_uri,
        }
        async with build_pinned_async_client(
            allowed_hosts=_ALLOWED_TOKEN_HOSTS,
            timeout=_TIMEOUT_SECONDS,
            resolver=self._resolver,
            trust_env=False,
        ) as client:
            return _token_body(await _send_token_request(client, data))


async def _send_token_request(client: httpx.AsyncClient, data: dict[str, str]) -> httpx.Response:
    """El estado se comprueba a mano en vez de con `raise_for_status()`: el
    `httpx.HTTPStatusError` que este construye imprime la URL entera."""
    try:
        response = await client.post(_TOKEN_ENDPOINT, data=data)
    except BlockedEgressAddressError as exc:
        raise GoogleOidcError("egreso hacia Google bloqueado") from exc
    except httpx.HTTPError as exc:
        raise GoogleOidcError("fallo de red al hablar con Google") from exc
    if response.status_code != httpx.codes.OK:
        raise GoogleOidcError(f"Google token endpoint HTTP {response.status_code}")
    return response


def _token_body(response: httpx.Response) -> dict[str, Any]:
    try:
        parsed = response.json()
    except ValueError:
        # `json.JSONDecodeError` guarda el documento entero en `.doc`; se
        # corta la cadena para que el cuerpo crudo no viaje con el error.
        raise GoogleOidcError("respuesta de Google no es JSON valido") from None
    if not isinstance(parsed, dict):
        raise GoogleOidcError("respuesta de Google no es un objeto JSON")
    return parsed


def _decode_jwt_payload_unverified(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != _JWT_SEGMENTS:
        raise GoogleOidcError("id_token mal formado")
    padding = "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    except (ValueError, TypeError):
        raise GoogleOidcError("payload del id_token invalido") from None
    if not isinstance(payload, dict):
        raise GoogleOidcError("payload del id_token no es un objeto")
    return payload
