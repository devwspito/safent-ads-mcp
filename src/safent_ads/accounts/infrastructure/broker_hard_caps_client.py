"""`BrokerHardCapsSocketClient`: implementa `HardCapsBrokerPort` sobre el
mismo socket unix que el resto (`$ADS_BROKER_SOCKET`), con el mismo framing
que `broker_client.py`/`oauth_broker_client.py` -- repetido a proposito y
no importado, por la misma razon que documenta el docstring de aquel: el
paquete `broker/presentation/` es del proceso `ads-broker` y no debe
aparecer en la ruta de importacion de `ads-api`.

**Sin reintento, nunca** (i11). `set_account_caps` y `delete_account_caps`
son MUTACIONES, igual que `execute_write`: este cliente no tiene siquiera
un parametro `retryable`, asi que no hay forma de pedirle que repita.
`resolve_account_caps` tampoco lo necesita -- una lectura de topes que
falla se resuelve mostrando el error, no insistiendo.

Ninguna forma que este cliente parsea puede llevar un secreto: solo
importes, procedencia y el sobre que el operador escribio en su fichero."""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.application.hard_caps_ports import (
    AccountHardCapsView,
    CapAmountsView,
    EffectiveAmountsView,
    EnvelopeView,
    HardCapsBrokerPort,
    PanelCapsInput,
)
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError

_LENGTH_PREFIX_FORMAT: Final = ">I"
_LENGTH_PREFIX_SIZE: Final = struct.calcsize(_LENGTH_PREFIX_FORMAT)
_MAX_FRAME_BYTES: Final = 64 * 1024
_DEFAULT_TIMEOUT_SECONDS: Final = 10.0

__all__ = ["BrokerHardCapsSocketClient"]


class BrokerHardCapsSocketClient(HardCapsBrokerPort):
    def __init__(
        self, socket_path: Path, *, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    ) -> None:
        self._socket_path = socket_path
        self._timeout_seconds = timeout_seconds

    async def resolve_account_caps(self, platform_account_id: str) -> AccountHardCapsView:
        return await self._view(
            {"op": "resolve_account_caps", "platform_account_id": platform_account_id}
        )

    async def set_account_caps(
        self,
        platform_account_id: str,
        caps: PanelCapsInput,
        *,
        requested_by: str,
        request_id: str | None = None,
    ) -> AccountHardCapsView:
        payload: dict[str, Any] = {
            "op": "set_account_caps",
            "platform_account_id": platform_account_id,
            "caps": {
                "daily_cap_minor": caps.daily_cap_minor,
                "monthly_cap_minor": caps.monthly_cap_minor,
                "ceiling_minor": caps.ceiling_minor,
                "currency": caps.currency,
            },
            "requested_by": requested_by,
        }
        if request_id is not None:
            payload["request_id"] = request_id
        return await self._view(payload)

    async def delete_account_caps(
        self, platform_account_id: str, *, requested_by: str, request_id: str | None = None
    ) -> AccountHardCapsView:
        payload: dict[str, Any] = {
            "op": "delete_account_caps",
            "platform_account_id": platform_account_id,
            "requested_by": requested_by,
        }
        if request_id is not None:
            payload["request_id"] = request_id
        return await self._view(payload)

    async def _view(self, payload: dict[str, Any]) -> AccountHardCapsView:
        """Una respuesta con la forma equivocada es un broker que no habla
        el contrato, no un exito a medias: se trata como fallo de conexion
        (503) en vez de reventar con un `KeyError` que la capa REST acabaria
        traduciendo a un 500 sin diagnostico."""
        try:
            return _parse_view(await self._request(payload))
        except (KeyError, TypeError) as exc:
            raise BrokerConnectionError("respuesta del broker fuera de contrato") from exc

    async def _request(self, payload: dict[str, Any]) -> Mapping[str, Any]:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self._socket_path)),
                timeout=self._timeout_seconds,
            )
        except (OSError, TimeoutError) as exc:
            raise BrokerConnectionError(f"no se pudo conectar a {self._socket_path}") from exc
        try:
            await self._write_frame(writer, json.dumps(payload).encode("utf-8"))
            raw = await asyncio.wait_for(self._read_frame(reader), timeout=self._timeout_seconds)
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
            raise BrokerConnectionError("fallo de E/S hablando con el broker") from exc
        finally:
            writer.close()
            await writer.wait_closed()
        return _unwrap(json.loads(raw))

    async def _write_frame(self, writer: asyncio.StreamWriter, payload: bytes) -> None:
        if len(payload) > _MAX_FRAME_BYTES:
            raise BrokerConnectionError("peticion mayor que el limite de trama")
        writer.write(struct.pack(_LENGTH_PREFIX_FORMAT, len(payload)) + payload)
        await writer.drain()

    async def _read_frame(self, reader: asyncio.StreamReader) -> bytes:
        header = await reader.readexactly(_LENGTH_PREFIX_SIZE)
        (length,) = struct.unpack(_LENGTH_PREFIX_FORMAT, header)
        if length > _MAX_FRAME_BYTES:
            raise BrokerConnectionError("respuesta mayor que el limite de trama")
        return await reader.readexactly(length)


def _unwrap(response: Any) -> Mapping[str, Any]:  # noqa: ANN401 - JSON crudo del socket
    if not isinstance(response, Mapping):
        raise TypeError("la respuesta del broker no es un objeto")
    if not response.get("ok"):
        raise BrokerRequestDeniedError(
            str(response.get("error_code", "UNKNOWN")), response.get("reason")
        )
    result: Mapping[str, Any] = response["result"]
    return result


def _parse_view(result: Mapping[str, Any]) -> AccountHardCapsView:
    return AccountHardCapsView(
        platform_account_id=result["platform_account_id"],
        source=result["source"],
        writable=result["writable"],
        effective=_parse_amounts(result.get("effective")),
        clamped_by=tuple(result.get("clamped_by") or ()),
        panel_state_available=bool(result.get("panel_state_available", True)),
        envelope=_parse_envelope(result.get("envelope")),
        from_file=_parse_cap_amounts(result.get("from_file")),
        from_panel=_parse_cap_amounts(result.get("from_panel")),
    )


def _parse_cap_amounts(raw: Mapping[str, Any] | None) -> CapAmountsView | None:
    if raw is None:
        return None
    return CapAmountsView(
        daily_cap_minor=raw["daily_cap_minor"],
        monthly_cap_minor=raw["monthly_cap_minor"],
        ceiling_minor=raw["ceiling_minor"],
    )


def _parse_amounts(raw: Mapping[str, Any] | None) -> EffectiveAmountsView | None:
    if raw is None:
        return None
    return EffectiveAmountsView(
        daily_cap_minor=raw["daily_cap_minor"],
        monthly_cap_minor=raw["monthly_cap_minor"],
        floor_minor=raw["floor_minor"],
        ceiling_minor=raw["ceiling_minor"],
    )


def _parse_envelope(raw: Mapping[str, Any] | None) -> EnvelopeView | None:
    if raw is None:
        return None
    return EnvelopeView(
        max_daily_cap_minor=raw["max_daily_cap_minor"],
        max_monthly_cap_minor=raw["max_monthly_cap_minor"],
        max_ceiling_minor=raw["max_ceiling_minor"],
        min_floor_minor=raw["min_floor_minor"],
        max_accounts=raw["max_accounts"],
        accounts_used=raw["accounts_used"],
        max_cap_changes_per_day=raw["max_cap_changes_per_day"],
        cap_changes_today=raw["cap_changes_today"],
        currency=raw["currency"],
    )
