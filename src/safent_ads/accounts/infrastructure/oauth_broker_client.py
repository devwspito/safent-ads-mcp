"""`OAuthBrokerSocketClient`: implementa `OAuthBrokerPort`
(accounts/application/connect_ports.py) serializando los 5 `op` de
conexion OAuth (US3) sobre `$ADS_BROKER_SOCKET`
(broker/presentation/request_schemas.py/serializers.py), y ademas
`PlatformAppsBrokerPort` (accounts/application/platform_apps_ports.py) con
los 3 `op` de credenciales de VENDOR (owner decision, app-credentials-ui)
sobre el MISMO socket -- una sola clase, un solo framing, dos puertos de
`accounts` hacia el mismo proceso de confianza. Vive en `ads-api`; no
conoce ningun SDK de plataforma ni credenciales -- solo la forma JSON que
el broker ya expone.

El framing (uint32 big-endian + JSON) se repite de `broker_client.py` a
proposito, no se importa: el mismo docstring de ese fichero documenta la
decision ("duplicarlas es mas barato que acoplar los dos procesos"). Las
respuestas que este cliente sabe parsear (`OAuthBeginResult`,
`OAuthCompleteResult`, `DiscoveredAccount`, `CredentialStatusResult`) no
tienen, por construccion, ningun campo capaz de llevar un token -- es la
prueba estructural de que `ads-api` nunca sostiene el secreto
(threat-model.md C-24)."""

from __future__ import annotations

import asyncio
import json
import struct
import uuid
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from safent_ads.accounts.application.connect_ports import (
    CredentialStatusResult,
    DiscoveredAccount,
    OAuthBeginResult,
    OAuthBrokerPort,
    OAuthCompleteResult,
)
from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.application.platform_apps_ports import (
    GoogleAppCredentialsInput,
    MetaAppCredentialsInput,
    PlatformAppsBrokerPort,
    PlatformAppStatus,
)
from safent_ads.accounts.domain.google_customer_id import normalize_google_customer_id
from safent_ads.accounts.domain.platform_account import ApiTier
from safent_ads.accounts.domain.platform_credential import CredentialStatus
from safent_ads.accounts.domain.refs import CredentialRefId
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.shared.ids import BusinessId, PlatformCode

_LENGTH_PREFIX_FORMAT: Final = ">I"
_LENGTH_PREFIX_SIZE: Final = struct.calcsize(_LENGTH_PREFIX_FORMAT)
_DEFAULT_MAX_FRAME_BYTES: Final = 512 * 1024
_DEFAULT_TIMEOUT_SECONDS: Final = 10.0
_COMPLETE_TIMEOUT_SECONDS: Final = 120.0


class OAuthBrokerSocketClient(OAuthBrokerPort, PlatformAppsBrokerPort):
    def __init__(
        self,
        socket_path: Path,
        *,
        max_frame_bytes: int = _DEFAULT_MAX_FRAME_BYTES,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        complete_timeout_seconds: float = _COMPLETE_TIMEOUT_SECONDS,
    ) -> None:
        self._socket_path = socket_path
        self._max_frame_bytes = max_frame_bytes
        self._timeout_seconds = timeout_seconds
        self._complete_timeout_seconds = complete_timeout_seconds

    async def composio_channel(self) -> dict[str, Any]:
        result = await self._request({"op": "composio_channel"})
        return {"version": result["version"], "public_key": result["public_key"]}

    async def accept_composio_lease(self, envelope: str) -> None:
        result = await self._request({"op": "composio_lease", "envelope": envelope})
        if result != {"accepted": True}:
            raise BrokerConnectionError("invalid_composio_lease_acknowledgment")

    async def begin(
        self,
        provider: PlatformCode,
        business_id: BusinessId,
        redirect_uri: str,
        *,
        owner_id: str | None = None,
        google_customer_id: str | None = None,
    ) -> OAuthBeginResult:
        customer_id = normalize_google_customer_id(google_customer_id, provider=provider)
        selection = {"google_customer_id": customer_id} if customer_id is not None else {}
        result = await self._request(
            {
                "op": "oauth_begin",
                "provider": provider.value,
                "business_id": str(business_id),
                "redirect_uri": redirect_uri,
                "owner_id": owner_id,
                **selection,
            }
        )
        return OAuthBeginResult(
            authorization_url=result["authorization_url"],
            state=result["state"],
            expires_at=_parse_datetime(result["expires_at"]),
            connection_id=result.get("connection_id"),
        )

    async def complete(self, state: str, code: str) -> OAuthCompleteResult:
        result = await self._request(
            {"op": "oauth_complete", "state": state, "code": code},
            response_timeout_seconds=self._complete_timeout_seconds,
        )
        return _parse_complete_result(result)

    async def credential_status(self, credential_ref_id: CredentialRefId) -> CredentialStatusResult:
        result = await self._request(
            {"op": "credential_status", "credential_ref_id": str(credential_ref_id)}
        )
        return CredentialStatusResult(
            status=CredentialStatus(result["status"]),
            scopes=frozenset(result["scopes"]),
            expires_at=_parse_optional_datetime(result["expires_at"]),
            last_validated_at=_parse_optional_datetime(result["last_validated_at"]),
        )

    async def revoke_credential(self, credential_ref_id: CredentialRefId) -> None:
        await self._request(
            {"op": "revoke_credential", "credential_ref_id": str(credential_ref_id)}
        )

    async def register_meta_system_user_token(
        self,
        token: str,
        *,
        business_id: str | None = None,
        owner_id: str | None = None,
    ) -> OAuthCompleteResult:
        result = await self._request(
            {
                "op": "register_meta_system_user_token",
                "token": token,
                "business_id": business_id,
                "owner_id": owner_id,
            }
        )
        return _parse_complete_result(result)

    async def set_google_app_credentials(
        self, credentials: GoogleAppCredentialsInput
    ) -> PlatformAppStatus:
        result = await self._request(
            {
                "op": "set_platform_app_credentials",
                "platform": PlatformCode.GOOGLE.value,
                "client_id": credentials.client_id,
                "client_type": credentials.client_type,
                "client_secret": credentials.client_secret,
                "login_customer_id": credentials.login_customer_id,
            }
        )
        return _parse_platform_app_status(result)

    async def set_meta_app_credentials(
        self, credentials: MetaAppCredentialsInput
    ) -> PlatformAppStatus:
        result = await self._request(
            {
                "op": "set_platform_app_credentials",
                "platform": PlatformCode.META.value,
                "app_id": credentials.app_id,
                "app_secret": credentials.app_secret,
            }
        )
        return _parse_platform_app_status(result)

    async def get_app_status(self, platform: PlatformCode) -> PlatformAppStatus:
        result = await self._request({"op": "get_platform_app_status", "platform": platform.value})
        return _parse_platform_app_status(result)

    async def delete_app_credentials(self, platform: PlatformCode) -> None:
        await self._request({"op": "delete_platform_app_credentials", "platform": platform.value})

    async def _request(  # noqa: ANN401 - JSON heterogeneo
        self, payload: dict[str, Any], *, response_timeout_seconds: float | None = None
    ) -> Any:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self._socket_path)), timeout=self._timeout_seconds
            )
        except (OSError, TimeoutError) as exc:
            raise BrokerConnectionError(f"no se pudo conectar a {self._socket_path}") from exc

        try:
            await self._write_frame(writer, json.dumps(payload).encode("utf-8"))
            raw_response = await asyncio.wait_for(
                self._read_frame(reader),
                timeout=(
                    self._timeout_seconds
                    if response_timeout_seconds is None
                    else response_timeout_seconds
                ),
            )
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
            raise BrokerConnectionError("fallo de E/S hablando con el broker") from exc
        finally:
            writer.close()
            await writer.wait_closed()

        return self._unwrap(json.loads(raw_response))

    def _unwrap(self, response: dict[str, Any]) -> Any:  # noqa: ANN401 - JSON heterogeneo
        if not response.get("ok"):
            raise BrokerRequestDeniedError(
                str(response.get("error_code", "UNKNOWN")), response.get("reason")
            )
        return response["result"]

    async def _write_frame(self, writer: asyncio.StreamWriter, payload: bytes) -> None:
        if len(payload) > self._max_frame_bytes:
            raise BrokerConnectionError("peticion mayor que el limite de trama")
        writer.write(struct.pack(_LENGTH_PREFIX_FORMAT, len(payload)) + payload)
        await writer.drain()

    async def _read_frame(self, reader: asyncio.StreamReader) -> bytes:
        header = await reader.readexactly(_LENGTH_PREFIX_SIZE)
        (length,) = struct.unpack(_LENGTH_PREFIX_FORMAT, header)
        if length > self._max_frame_bytes:
            raise BrokerConnectionError("respuesta mayor que el limite de trama")
        return await reader.readexactly(length)


def _parse_complete_result(result: Mapping[str, Any]) -> OAuthCompleteResult:
    return OAuthCompleteResult(
        accounts=[_parse_discovered_account(item) for item in result["accounts"]]
    )


def _parse_discovered_account(raw: Mapping[str, Any]) -> DiscoveredAccount:
    return DiscoveredAccount(
        business_id=raw.get("business_id"),
        connection_id=raw.get("connection_id"),
        owner_id=raw.get("owner_id"),
        platform=PlatformCode(raw["platform"]),
        external_account_id=raw["external_account_id"],
        label=raw["label"],
        currency=raw["currency"],
        timezone=raw["timezone"],
        api_tier=ApiTier(raw["api_tier"]),
        credential_ref_id=CredentialRefId(uuid.UUID(raw["credential_ref_id"])),
        scopes=frozenset(raw["scopes"]),
        obtained_at=_parse_datetime(raw["obtained_at"]),
        expires_at=_parse_optional_datetime(raw["expires_at"]),
    )


def _parse_platform_app_status(raw: Mapping[str, Any]) -> PlatformAppStatus:
    return PlatformAppStatus(
        platform=PlatformCode(raw["platform"]),
        configured=raw["configured"],
        client_id_masked=raw["client_id_masked"],
        login_customer_id_masked=raw["login_customer_id_masked"],
        updated_at=_parse_optional_datetime(raw["updated_at"]),
        client_type=raw.get("client_type"),
    )


def _parse_datetime(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def _parse_optional_datetime(raw: str | None) -> datetime | None:
    return None if raw is None else _parse_datetime(raw)
