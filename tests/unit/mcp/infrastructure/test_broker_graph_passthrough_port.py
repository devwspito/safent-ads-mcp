"""B-1 (revision de seguridad de las herramientas sensibles del MCP): la
proyeccion a `fields` es obligatoria en el CLIENTE, no solo en el bróker --
si este ultimo devolviera columnas de mas (bug, `field{subcampo}`, un
cambio de contrato), nunca deben llegar al llamante MCP.

Incidente de produccion (companion 0.2.21): una denegacion del bróker
distinta de `ENTITY_NOT_FOUND` (p.ej. `PLATFORM_APP_NOT_CONFIGURED`)
escapaba como `BrokerRequestDeniedError` crudo -- el SDK MCP la convertia
en el `UnexpectedToolError` opaco en vez del sobre limpio del contrato."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.broker.presentation.wire_protocol import FrameClient
from safent_ads.mcp.application.errors import BrokerUnavailableError, PlatformAppNotConfiguredError
from safent_ads.mcp.infrastructure import broker_graph_passthrough_port as module
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import PlatformCode

_NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)
_BUSINESS_ID = "11111111-1111-1111-1111-111111111111"
_PROVIDER_DETAIL = "composio_app_id=secret-internal-value"


def _port(client: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> module.BrokerGraphPassthroughPort:
    account = AccountRef(PlatformCode.META, "act_123")
    monkeypatch.setattr(module, "resolve_owned_account_ref", AsyncMock(return_value=account))
    return module.BrokerGraphPassthroughPort(client, object(), FixedClock(_NOW))


async def test_la_respuesta_se_proyecta_aunque_el_broker_devuelva_campos_de_mas(
    monkeypatch,
) -> None:
    account = AccountRef(PlatformCode.META, "act_123")
    monkeypatch.setattr(module, "resolve_owned_account_ref", AsyncMock(return_value=account))
    client = AsyncMock()
    client.get_meta_graph.return_value = [
        {"id": "1", "name": "Campana", "access_token": "EAA-filtrado-en-la-arista-no-en-fields"}
    ]
    port = module.BrokerGraphPassthroughPort(client, object(), FixedClock(_NOW))

    result = await port.get_meta_graph(
        "business-1",
        "meta:act_123",
        node="123",
        edge="campaigns",
        fields=("id", "name"),
        params={},
    )

    assert result.rows == ({"id": "1", "name": "Campana"},)


async def test_a_denial_other_than_entity_not_found_translates_to_a_typed_error(
    monkeypatch,
) -> None:
    client = AsyncMock()
    client.get_meta_graph.side_effect = BrokerRequestDeniedError(
        "PLATFORM_APP_NOT_CONFIGURED", _PROVIDER_DETAIL
    )
    port = _port(client, monkeypatch)

    with pytest.raises(PlatformAppNotConfiguredError) as excinfo:
        await port.get_meta_graph(
            _BUSINESS_ID, "meta:act_123", node="123", edge="campaigns", fields=("id",), params={}
        )

    assert excinfo.value.code == "PLATFORM_APP_NOT_CONFIGURED"
    assert _PROVIDER_DETAIL not in str(excinfo.value)


async def test_an_unmapped_denial_code_is_reraised_unwrapped(monkeypatch) -> None:
    client = AsyncMock()
    error = BrokerRequestDeniedError("SOME_FUTURE_BROKER_CODE", _PROVIDER_DETAIL)
    client.get_meta_graph.side_effect = error
    port = _port(client, monkeypatch)

    with pytest.raises(BrokerRequestDeniedError) as excinfo:
        await port.get_meta_graph(
            _BUSINESS_ID, "meta:act_123", node="123", edge="campaigns", fields=("id",), params={}
        )

    assert excinfo.value is error


async def test_a_transport_failure_becomes_broker_unavailable(monkeypatch) -> None:
    client = AsyncMock()
    client.get_meta_graph.side_effect = BrokerConnectionError("no se pudo conectar al socket")
    port = _port(client, monkeypatch)

    with pytest.raises(BrokerUnavailableError) as excinfo:
        await port.get_meta_graph(
            _BUSINESS_ID, "meta:act_123", node="123", edge="campaigns", fields=("id",), params={}
        )

    assert excinfo.value.code == "BROKER_UNAVAILABLE"
    assert "socket" not in str(excinfo.value)


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


async def test_get_meta_graph_retries_once_on_a_cold_broker(tmp_path: Path) -> None:
    """Regresion (16-sep, companion 0.2.32): `meta_graph_get` es una
    lectura pura (`get_node`/`get_edge`) -- debe beneficiarse del mismo
    reintento que `broker_reference_data_port.py` sin necesitar un segundo
    intento manual del llamante."""
    socket_path = tmp_path / "broker.sock"
    server, calls = await _serve_cold_start(
        socket_path,
        slow_seconds=1.0,
        response={"ok": True, "result": {"rows": [{"id": "1", "name": "Campana"}]}},
    )
    try:
        client = module.GraphPassthroughBrokerClient(socket_path)
        client._timeout_seconds = 0.2  # noqa: SLF001 - pequeno a proposito, ver docstring de arriba
        rows = await client.get_meta_graph(
            business_id="business-1",
            external_account_id="act_123",
            node="123",
            edge="campaigns",
            fields=("id", "name"),
            params={},
        )
    finally:
        server.close()
        await server.wait_closed()

    assert rows == [{"id": "1", "name": "Campana"}]
    assert calls[0] == 2
