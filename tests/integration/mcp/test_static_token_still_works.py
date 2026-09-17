"""No regresion de P3 (tasks.md T021, threat-model.md C-53): con
`ADS_MCP_OAUTH_ENABLED` en su valor por defecto (`true`) y
`ADS_MCP_STATIC_TOKEN_ENABLED` encendido a proposito (M6 de la revision de
seguridad, 16-sep: `false` es el valor por defecto desde que el instalador
y ambos agentes usan OAuth), `ADS_MCP_TOKEN` sigue abriendo una sesion MCP
y `GET /mcp/health` sigue respondiendo.

A diferencia de `tests/integration/mcp/test_streamable_http_endpoint.py`
(que apaga OAuth a proposito para no tocar Postgres), este fichero SI trae
un Postgres real: con OAuth encendido, `CompositeTokenVerifier` intenta
primero `IntrospectToken` (una consulta real a `oauth_grants`) antes de
caer al estatico (`shared/bearer.py::is_token_valid`) -- el camino que este
test ejercita es justo ese fallback, no el atajo sin base de datos.

Fusion lane/003: el bearer estatico abre `/mcp` en el MODO DE UN SOLO
PROPIETARIO (`ADS_SINGLE_OWNER_MODE=true`, el Safent local del dueno /
motor Hermes), que es donde `ADS_MCP_TOKEN` sigue siendo la credencial. Con
`ADS_SEAT_AUTHORITY_ENABLED=true` (produccion alojada) `/mcp` exige un
puesto de Enterprise o una concesion OAuth, y el estatico solo sirve para
`GET /mcp/health` -- eso es 004 tasks.md A6, no una regresion de P3."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from tests.unit.composition.factories import build_api_settings

from safent_ads.composition.app import create_app

pytestmark = pytest.mark.integration

_BASE_URL = "https://ads.test.ts.net"
_ENDPOINT = f"{_BASE_URL}/mcp"
_STATIC_TOKEN = "test-mcp-token-abc123"  # noqa: S105 - el de `build_api_settings`, no un secreto real


@asynccontextmanager
async def _mcp_http_client(database_url: str) -> AsyncIterator[httpx2.AsyncClient]:
    app = create_app(
        build_api_settings(
            database_url=database_url,
            mcp_static_token_enabled=True,
            seat_authority_enabled=False,
            single_owner_mode=True,
        )
    )
    try:
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app),
                base_url=_BASE_URL,
                headers={"Authorization": f"Bearer {_STATIC_TOKEN}"},
            ) as client:
                yield client
    finally:
        await app.state.container.aclose()


async def test_static_token_opens_an_mcp_session_with_oauth_enabled_by_default(
    database_url: str,
) -> None:
    async with _mcp_http_client(database_url) as client:
        async with streamable_http_client(_ENDPOINT, http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                initialize_result = await session.initialize()
                tools_result = await session.list_tools()

    # 004 tasks.md A6: un `MCPServer` por permiso (`ads-view`/`ads-propose`/
    # `ads-approve`); el modo de un solo propietario enruta al de `aprobar`.
    assert initialize_result.server_info.name == "ads-approve"
    assert [tool.name for tool in tools_result.tools], "el catalogo de herramientas llego vacio"


async def test_static_token_still_answers_mcp_health_with_oauth_enabled_by_default(
    database_url: str,
) -> None:
    async with _mcp_http_client(database_url) as client:
        response = await client.get("/mcp/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"]
    assert body["contract_version"]
