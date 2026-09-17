"""Servidor del socket Unix del broker: `SO_PEERCRED` -> trama -> despacho
(T027). Enruta las tres operaciones de lectura de US1 y las cinco de
conexion OAuth de US3; `execute_write` sigue `DENIED` porque
`dispatcher.handle_payload` no tiene esquema para ella (el chokepoint que
la autoriza llega en F2, plan.md §10)."""

from __future__ import annotations

import asyncio
import json
import os
import traceback
from pathlib import Path
from typing import Final

import structlog

from safent_ads.broker.infrastructure.peer_credentials import is_allowed_peer, peer_uid
from safent_ads.broker.presentation.dispatcher import BrokerRuntime, handle_payload
from safent_ads.broker.presentation.wire_protocol import (
    FrameClient,
    FrameTooLargeError,
    op_prefix_pattern,
)

logger = structlog.get_logger(__name__)

_DEFAULT_MAX_FRAME_BYTES: Final = 64 * 1024
_DEFAULT_READ_TIMEOUT_SECONDS: Final = 10.0
# `handle_payload` never fail-opens BY CONTRACT (contracts/platform-port.md),
# but nothing before enforced that -- an exception raised while building its
# own `ok: false` response (e.g. `_ok_response`'s `json.dumps`, outside
# `dispatcher._dispatch`'s own try/except) used to escape all the way out of
# this coroutine, the `client_connected_cb` of `asyncio.start_unix_server`:
# an "Unhandled exception in client_connected_cb" that just drops the
# client's connection instead of answering it (fix/broker-ad-library-
# typeerror). This is the last-resort frame for exactly that case.
_INTERNAL_ERROR_RESPONSE: Final = json.dumps(
    {"ok": False, "error_code": "FAILED", "reason": "broker_request_crashed"}
).encode("utf-8")
_LOGGED_OP_MAX_CHARS: Final = 64

# M-3 (revision de seguridad 0.2.22): `render_image` devuelve una imagen en
# base64 sobre el sobre JSON EN LA RESPUESTA -- un JPEG/PNG de anuncio
# tipico (hasta 1080x1920, el formato mas grande de `creative.domain.enums.
# Format`) mas el +33% del propio base64 supera de sobra los 64 KiB por
# defecto. `upload_asset` es al reves: los bytes van EN LA PETICION
# (subida de un activo al proveedor). Antes esto se resolvia subiendo
# `max_frame_bytes` para TODO el socket (`composition/broker.py`), lo que
# tambien dejaba pasar una peticion de 16 MiB para cualquier otra `op`
# (`list_meta_pages` incluida) antes siquiera de mirar que operacion era --
# una superficie de denegacion de servicio nueva. Ahora el limite grande es
# SOLO para estas dos operaciones, en cada direccion que de verdad lo
# necesita: lectura via `FrameClient.read_op_scoped_frame` (sondea el
# prefijo `{"op": "..."}`, nunca hace falta parsear el JSON completo para
# decidir cuanto leer todavia) y escritura via `write_frame(max_bytes=...)`
# ya con la `op` conocida (la peticion entrante ya se parseo en
# `handle_payload`).
_MAX_EXTENDED_FRAME_BYTES: Final = 16 * 1024 * 1024
_EXTENDED_FRAME_OPS: Final = ("render_image", "upload_asset")
_EXTENDED_OP_PATTERN: Final = op_prefix_pattern(*_EXTENDED_FRAME_OPS)
_PER_OP_MAX_RESPONSE_BYTES: Final[dict[str, int]] = dict.fromkeys(
    _EXTENDED_FRAME_OPS, _MAX_EXTENDED_FRAME_BYTES
)


def _max_response_bytes_for(op: str | None) -> int:
    if op is None:
        return _DEFAULT_MAX_FRAME_BYTES
    return _PER_OP_MAX_RESPONSE_BYTES.get(op, _DEFAULT_MAX_FRAME_BYTES)


def _op_from_raw(raw: bytes) -> str | None:
    """Lectura best-effort, solo para elegir el limite de la RESPUESTA --
    `handle_payload` sigue siendo la unica fuente de verdad que valida y
    rechaza el payload; un `raw` ilegible aqui simplemente cae al limite
    por defecto, nunca se trata como un error."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    op = payload.get("op") if isinstance(payload, dict) else None
    return op if isinstance(op, str) else None


async def _close(frame_client: FrameClient) -> None:
    frame_client.close()
    await frame_client.wait_closed()


def _crash_location(exc: BaseException) -> str | None:
    """Deepest `safent_ads` frame only, `<module>:<lineno>` -- never the
    exception message (may carry provider bodies or tokens) and never a
    full filesystem path. `logging_setup.py::_exception_type_only` pops
    `exc_info` before rendering in production, collapsing it to
    `exception_type` (the same information `error_type` below already
    carries) with no location at all -- this is our own breadcrumb for
    exactly that gap."""
    frames = [
        frame for frame in traceback.extract_tb(exc.__traceback__) if "safent_ads" in frame.filename
    ]
    if not frames:
        return None
    frame = frames[-1]
    return f"{Path(frame.filename).stem}:{frame.lineno}"


async def _handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    runtime: BrokerRuntime,
    allowed_uids: frozenset[int],
    max_frame_bytes: int,
    read_timeout_seconds: float,
    self_uid: int,
) -> None:
    if not is_allowed_peer(writer, allowed_uids):
        uid = peer_uid(writer)
        # The broker's own healthcheck connects to its own socket (peer_uid
        # == our uid) every ~15s and is never in `allowed_uids` -- benign,
        # expected noise, not an intrusion attempt. Anything else stays a
        # WARNING.
        log = logger.debug if uid == self_uid else logger.warning
        log("broker_peer_rejected", peer_uid=uid)
        writer.close()
        await writer.wait_closed()
        return

    frame_client = FrameClient(reader, writer, max_frame_bytes=max_frame_bytes)
    try:
        raw = await asyncio.wait_for(
            frame_client.read_op_scoped_frame(
                extended_max_bytes=_MAX_EXTENDED_FRAME_BYTES,
                extended_op_pattern=_EXTENDED_OP_PATTERN,
            ),
            timeout=read_timeout_seconds,
        )
    except (TimeoutError, ConnectionError, asyncio.IncompleteReadError, FrameTooLargeError):
        await _close(frame_client)
        return

    op = _op_from_raw(raw)
    try:
        response = await handle_payload(raw, runtime)
    except Exception as exc:  # noqa: BLE001 - last-resort net, must never drop the connection
        # `op` is attacker-controlled and unbounded (`_op_from_raw` only
        # checks it is a string) -- truncate before it ever reaches a log
        # line. Class name, truncated op and `_crash_location` only: `raw`/
        # `exc`'s own message may carry provider bodies or tokens, same
        # rule `_dispatch` already applies to its own known-error branch.
        logger.error(
            "broker_request_crashed",
            op=op[:_LOGGED_OP_MAX_CHARS] if op is not None else None,
            error_type=type(exc).__name__,
            where=_crash_location(exc),
            exc_info=True,
        )
        response = _INTERNAL_ERROR_RESPONSE
    try:
        await frame_client.write_frame(response, max_bytes=_max_response_bytes_for(op))
    except (ConnectionError, FrameTooLargeError):
        pass
    await _close(frame_client)


def _prepare_socket_path(socket_path: Path) -> None:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()


async def serve(
    socket_path: Path,
    runtime: BrokerRuntime,
    allowed_uids: frozenset[int],
    *,
    self_uid: int | None = None,
    max_frame_bytes: int = _DEFAULT_MAX_FRAME_BYTES,
    read_timeout_seconds: float = _DEFAULT_READ_TIMEOUT_SECONDS,
) -> asyncio.Server:
    """Arranca el servidor y devuelve el `asyncio.Server` ya escuchando
    (permisos de socket y cableado de credenciales: composicion del proceso
    `ads-broker`, fuera de este lane). `self_uid` (`composition/broker.py`
    pasa `os.getuid()`) identifica al bróker frente a su propio healthcheck
    -- `None` solo cae en `os.getuid()` aqui para los llamantes (sobre todo
    de test) que no lo declaran explicito."""
    resolved_self_uid = self_uid if self_uid is not None else os.getuid()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handle_connection(
            reader,
            writer,
            runtime=runtime,
            allowed_uids=allowed_uids,
            max_frame_bytes=max_frame_bytes,
            read_timeout_seconds=read_timeout_seconds,
            self_uid=resolved_self_uid,
        )

    _prepare_socket_path(socket_path)
    return await asyncio.start_unix_server(handler, path=str(socket_path))
