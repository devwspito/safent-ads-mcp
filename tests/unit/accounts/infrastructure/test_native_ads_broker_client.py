"""`NativeAdsBrokerClient`: `native_ads_read` es de solo lectura por diseño
(`NativeMcpReadGateway`/`native_mcp_policy.py`: catalogo con
`"writes_exposed": False` y una lista blanca de herramientas
get/search/metadata) -- comparte el mismo `BrokerSocketClient._request`
(y su reintento en frio) que el resto de lecturas del bróker."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.accounts.infrastructure.native_ads_broker_client import NativeAdsBrokerClient
from safent_ads.broker.presentation.wire_protocol import FrameClient
from safent_ads.shared.ids import PlatformCode


async def _serve_cold_start(
    socket_path: Path, *, slow_seconds: float, response: dict[str, object]
) -> tuple[asyncio.Server, list[int]]:
    """Servidor de pega: la PRIMERA peticion tarda `slow_seconds` en
    responder (arranque en frio, incidente de produccion 16-sep -- el
    bróker construyendo en caliente el adaptador/cliente SDK todavia sin
    usar) -- lo bastante para superar el timeout, pequeno a proposito, del
    cliente. Desde la segunda peticion responde al instante, como el
    bróker ya en caliente."""
    calls: list[int] = [0]

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        frame_client = FrameClient(reader, writer, max_frame_bytes=64 * 1024)
        await frame_client.read_frame()
        calls[0] += 1
        if calls[0] == 1:
            await asyncio.sleep(slow_seconds)
        await frame_client.write_frame(json.dumps(response).encode("utf-8"))
        frame_client.close()
        await frame_client.wait_closed()

    server = await asyncio.start_unix_server(handler, path=str(socket_path))
    return server, calls


async def test_read_native_tool_retries_once_on_a_cold_broker(tmp_path: Path) -> None:
    """Regresion (16-sep, companion 0.2.32): `native_ads_read` nunca
    expone una escritura (`writes_exposed: False`, lista blanca de solo
    lectura en `native_mcp_policy.py`) -- debe beneficiarse del mismo
    reintento que el resto de lecturas de `BrokerSocketClient`."""
    socket_path = tmp_path / "broker.sock"
    server, calls = await _serve_cold_start(
        socket_path,
        slow_seconds=1.0,
        response={"ok": True, "result": {"rows": [{"resource_name": "customers/1/campaigns/1"}]}},
    )
    try:
        client = NativeAdsBrokerClient(socket_path)
        client._timeout_seconds = 0.2  # noqa: SLF001 - pequeno a proposito, ver docstring de arriba
        result = await client.read_native_tool(
            AccountRef(PlatformCode.GOOGLE, "1234567890"), "search_search", {"query": "SELECT 1"}
        )
    finally:
        server.close()
        await server.wait_closed()

    assert result == {"rows": [{"resource_name": "customers/1/campaigns/1"}]}
    assert calls[0] == 2
