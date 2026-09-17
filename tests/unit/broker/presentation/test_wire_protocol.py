"""`wire_protocol.py` (M-3, revision de seguridad 0.2.22): `op_prefix_pattern`
y `FrameClient.read_op_scoped_frame` en aislamiento, sin socket real --
`test_socket_server.py` ya cubre el camino completo end-to-end."""

from __future__ import annotations

import asyncio
import struct

import pytest

from safent_ads.broker.presentation.wire_protocol import (
    FrameClient,
    FrameTooLargeError,
    op_prefix_pattern,
)

_LENGTH_PREFIX_FORMAT = ">I"


def _framed(payload: bytes) -> bytes:
    return struct.pack(_LENGTH_PREFIX_FORMAT, len(payload)) + payload


def _reader_with(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


class _NullWriter:
    """Doble minimo -- estos tests nunca escriben, solo leen."""

    def write(self, data: bytes) -> None:  # noqa: ARG002 - nunca se llama
        raise AssertionError

    async def drain(self) -> None:  # pragma: no cover - nunca se llama
        raise AssertionError

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


def test_op_prefix_pattern_matches_the_space_json_dumps_leaves_by_default() -> None:
    pattern = op_prefix_pattern("render_image", "upload_asset")

    assert pattern.match(b'{"op": "render_image", "prompt": "x"}')


def test_op_prefix_pattern_matches_compact_serialization_too() -> None:
    pattern = op_prefix_pattern("render_image", "upload_asset")

    assert pattern.match(b'{"op":"upload_asset","file_name":"x"}')


def test_op_prefix_pattern_rejects_an_op_outside_the_allow_list() -> None:
    pattern = op_prefix_pattern("render_image", "upload_asset")

    assert not pattern.match(b'{"op": "meta_reference_read"}')


def test_op_prefix_pattern_rejects_op_that_is_not_the_first_key() -> None:
    """El orden importa: `op` tiene que ser la PRIMERA clave -- una trama
    que la manda en otra posicion no cuela, aunque el valor sea uno de los
    permitidos."""
    pattern = op_prefix_pattern("render_image", "upload_asset")

    assert not pattern.match(b'{"business_id": "biz-1", "op": "render_image"}')


async def test_read_op_scoped_frame_reads_a_small_frame_normally() -> None:
    payload = b'{"op": "meta_reference_read"}'
    reader = _reader_with(_framed(payload))
    client = FrameClient(reader, _NullWriter(), max_frame_bytes=64)  # type: ignore[arg-type]

    result = await client.read_op_scoped_frame(
        extended_max_bytes=1024, extended_op_pattern=op_prefix_pattern("render_image")
    )

    assert result == payload


async def test_read_op_scoped_frame_accepts_an_oversized_frame_with_an_allowed_prefix() -> None:
    probe_size = 32
    payload = b'{"op": "render_image"}' + b"x" * 100
    reader = _reader_with(_framed(payload))
    client = FrameClient(reader, _NullWriter(), max_frame_bytes=probe_size)  # type: ignore[arg-type]

    result = await client.read_op_scoped_frame(
        extended_max_bytes=1024, extended_op_pattern=op_prefix_pattern("render_image")
    )

    assert result == payload


async def test_read_op_scoped_frame_rejects_an_oversized_frame_with_a_disallowed_prefix() -> None:
    probe_size = 32
    payload = b'{"op": "meta_reference_read"}' + b"x" * 100
    reader = _reader_with(_framed(payload))
    client = FrameClient(reader, _NullWriter(), max_frame_bytes=probe_size)  # type: ignore[arg-type]

    with pytest.raises(FrameTooLargeError):
        await client.read_op_scoped_frame(
            extended_max_bytes=1024, extended_op_pattern=op_prefix_pattern("render_image")
        )


async def test_read_op_scoped_frame_rejects_a_ten_mebibyte_frame_with_a_disallowed_prefix() -> None:
    """Mismo rechazo con una trama de 10 MiB de basura -- el tamano total
    nunca importa, solo el prefijo: la sonda de `max_frame_bytes` basta
    para decidir sin leer el resto."""
    probe_size = 32
    payload = b'{"op": "meta_reference_read"}' + b"x" * (10 * 1024 * 1024)
    reader = _reader_with(_framed(payload))
    client = FrameClient(reader, _NullWriter(), max_frame_bytes=probe_size)  # type: ignore[arg-type]

    with pytest.raises(FrameTooLargeError):
        await client.read_op_scoped_frame(
            extended_max_bytes=16 * 1024 * 1024,
            extended_op_pattern=op_prefix_pattern("render_image"),
        )


async def test_read_op_scoped_frame_rejects_when_even_the_extended_ceiling_is_exceeded() -> None:
    probe_size = 32
    payload = b'{"op": "render_image"}' + b"x" * 100
    reader = _reader_with(_framed(payload))
    client = FrameClient(reader, _NullWriter(), max_frame_bytes=probe_size)  # type: ignore[arg-type]

    with pytest.raises(FrameTooLargeError):
        await client.read_op_scoped_frame(
            extended_max_bytes=50, extended_op_pattern=op_prefix_pattern("render_image")
        )
