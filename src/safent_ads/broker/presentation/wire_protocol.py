"""Trama del protocolo del broker (contracts/platform-port.md: "`uint32
big-endian` con la longitud + JSON UTF-8"). Sin logica de negocio: solo la
serializacion de bytes en el socket Unix. La usan tanto el servidor
(`socket_server.py`) como el cliente (`accounts/infrastructure/broker_client.py`)
para no duplicar el framing."""

from __future__ import annotations

import asyncio
import re
import struct
from typing import Final

from safent_ads.shared.errors import InfrastructureError

_LENGTH_PREFIX_FORMAT: Final = ">I"
_LENGTH_PREFIX_SIZE: Final = struct.calcsize(_LENGTH_PREFIX_FORMAT)


class FrameTooLargeError(InfrastructureError):
    """La trama declara una longitud mayor que el limite permitido."""


def op_prefix_pattern(*ops: str) -> re.Pattern[bytes]:
    """M-3 (revision de seguridad 0.2.22): `op` viaja como la PRIMERA
    clave del objeto JSON en todo constructor de payload de este repo
    (`{"op": "...", ...}` literal, nunca ensamblado a partir de un dict ya
    construido) -- comprobar el PREFIJO de bytes de la trama basta para
    saber la operacion sin parsear el JSON completo. Tolera el espacio
    que `json.dumps` por defecto deja tras `:` (y cualquier otro
    whitespace JSON valido ahi): un prefijo exacto sin tolerancia
    rompería en cuanto alguien cambiara de `json.dumps` por defecto a
    separadores compactos, o viceversa."""
    alternatives = "|".join(re.escape(op) for op in ops)
    return re.compile(rb'^\{\s*"op"\s*:\s*"(?:' + alternatives.encode() + rb')"')


class FrameClient:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        max_frame_bytes: int,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._max_frame_bytes = max_frame_bytes

    async def read_frame(self) -> bytes:
        header = await self._reader.readexactly(_LENGTH_PREFIX_SIZE)
        (length,) = struct.unpack(_LENGTH_PREFIX_FORMAT, header)
        if length > self._max_frame_bytes:
            raise FrameTooLargeError(f"trama de {length} bytes supera el limite")
        return await self._reader.readexactly(length)

    async def read_op_scoped_frame(
        self, *, extended_max_bytes: int, extended_op_pattern: re.Pattern[bytes]
    ) -> bytes:
        """M-3: limite de trama POR-OPERACION en la lectura. Una trama que
        no supera `self._max_frame_bytes` se lee igual que `read_frame`;
        una mayor solo se admite hasta `extended_max_bytes` SI sus
        primeros `self._max_frame_bytes` bytes encajan con
        `extended_op_pattern` (ver `op_prefix_pattern`) -- cualquier otro
        contenido, o una trama que se pasa incluso de `extended_max_bytes`,
        se rechaza sin leer ni un byte mas alla de la sonda inicial."""
        header = await self._reader.readexactly(_LENGTH_PREFIX_SIZE)
        (length,) = struct.unpack(_LENGTH_PREFIX_FORMAT, header)
        if length <= self._max_frame_bytes:
            return await self._reader.readexactly(length)
        probe = await self._reader.readexactly(self._max_frame_bytes)
        if length > extended_max_bytes or not extended_op_pattern.match(probe):
            raise FrameTooLargeError(f"trama de {length} bytes supera el limite")
        rest = await self._reader.readexactly(length - self._max_frame_bytes)
        return probe + rest

    async def write_frame(self, payload: bytes, *, max_bytes: int | None = None) -> None:
        if max_bytes is not None and len(payload) > max_bytes:
            raise FrameTooLargeError(f"respuesta de {len(payload)} bytes supera el limite")
        self._writer.write(struct.pack(_LENGTH_PREFIX_FORMAT, len(payload)) + payload)
        await self._writer.drain()

    def close(self) -> None:
        self._writer.close()

    async def wait_closed(self) -> None:
        await self._writer.wait_closed()
